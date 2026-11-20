from __future__ import annotations

import threading
import time
from pathlib import Path

from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango

from ..archives.base import Entry
from ..downloader import (
    DownloadJob,
    DownloadOptions,
    describe_missing_space,
    free_space,
)
from ..settings import SettingsStore
from ..util import (
    archive_folder_name,
    human_count,
    human_eta,
    human_size,
    human_speed,
    tilde_path,
)
from . import name_icon
from .alert import TrawlAlert
from ..notifications import play_sound, send_notification

_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "tif", "tiff", "avif", "heic", "ico", "raw"}
_VIDEO_EXTENSIONS = {"mp4", "mkv", "webm", "avi", "mov", "m4v", "mpg", "mpeg", "wmv", "flv", "ts", "m2ts"}
_AUDIO_EXTENSIONS = {"mp3", "flac", "ogg", "opus", "m4a", "wav", "aac", "wma", "aiff", "mid"}
_TEXT_EXTENSIONS = {"txt", "md", "pdf", "json", "xml", "csv", "html", "css", "js", "py", "c", "h", "cpp", "rs", "go", "toml", "yaml", "yml", "ini", "log", "nfo", "srt"}
_EXECUTABLE_EXTENSIONS = {"exe", "msi", "deb", "rpm", "appimage", "apk", "run", "bin", "sh"}


def _icon_for(path: str, is_dir: bool) -> str:
    if is_dir:
        return "folder-symbolic"
    extension = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
    if extension in _IMAGE_EXTENSIONS:
        return "image-x-generic-symbolic"
    if extension in _VIDEO_EXTENSIONS:
        return "video-x-generic-symbolic"
    if extension in _AUDIO_EXTENSIONS:
        return "audio-x-generic-symbolic"
    if extension in _TEXT_EXTENSIONS:
        return "text-x-generic-symbolic"
    if extension in _EXECUTABLE_EXTENSIONS:
        return "application-x-executable-symbolic"
    return "package-x-generic-symbolic"


def _nearest_existing(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


class EntryObject(GObject.Object):
    __gtype_name__ = "TrawlEntryObject"

    selected = GObject.Property(type=bool, default=False)

    def __init__(self, entry: Entry, index: int):
        super().__init__()
        self.entry = entry
        self.index = index


class ArchivePage(Gtk.Box):
    """The file browser and download panel for one open archive."""

    def __init__(self, window, session):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("nb-root")
        self._toolbar = Adw.ToolbarView()
        self._toolbar.set_vexpand(True)
        self.append(self._toolbar)
        self.window = window
        self.launch_log = window.launch_log
        self.session = session
        self.archive = session.archive
        self.info = session.info
        self._store: SettingsStore = window.store
        self._settings = self._store.settings
        self._alive = True
        self._job: DownloadJob | None = None
        self._downloading = False
        self._download_mode = "selected"
        self._download_started_at = 0.0
        self._last_progress_log = 0.0
        self._objects: list[EntryObject] = []
        self._selected: set[EntryObject] = set()
        self._bulk_select = False

        files = self.archive.files
        self._total_files = len(files)
        self._total_size = sum(entry.size for entry in files)
        self._choice_available = self._total_files > 1 and not self._settings.choice_skip
        self._mode = "choice" if self._choice_available else "browser"

        archive_name = self.info.filename or "archive"
        self._destination = self._default_destination(archive_name)

        # The window owns the single header bar (title, "+" and archive menu);
        # this page only contributes the filter bar, file list and bottom bar.

        # ------------------------------------------------------------- toolbar
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toolbar.set_margin_top(8)
        toolbar.set_margin_bottom(8)
        toolbar.set_margin_start(12)
        toolbar.set_margin_end(12)

        self._search = Gtk.SearchEntry()
        self._search.add_css_class("nb-input")
        self._search.set_placeholder_text("Filter by name or folder")
        self._search.set_hexpand(True)
        self._search.connect("search-changed", lambda *_: self._refilter())
        toolbar.append(self._search)

        self._select_all_button = Gtk.Button(label="Select all")
        self._select_all_button.add_css_class("nb-secondary")
        self._select_all_button.set_tooltip_text("Select every visible file")
        self._select_all_button.connect("clicked", lambda *_: self._select_all())
        toolbar.append(self._select_all_button)

        self._clear_button = Gtk.Button(label="Clear")
        self._clear_button.add_css_class("nb-secondary")
        self._clear_button.add_css_class("nb-orange")
        self._clear_button.set_tooltip_text("Clear the selection")
        self._clear_button.connect("clicked", lambda *_: self._clear_selection())
        toolbar.append(self._clear_button)

        self._selection_label = Gtk.Label(label="")
        self._selection_label.add_css_class("nb-tag")
        self._selection_label.set_margin_start(6)
        toolbar.append(self._selection_label)

        self._filter_bar = toolbar
        self._toolbar.add_top_bar(self._filter_bar)

        # --------------------------------------------------------------- list
        self._list_store = Gio.ListStore(item_type=EntryObject)
        self._filter = Gtk.CustomFilter.new(self._matches_filter, None)
        self._filter_model = Gtk.FilterListModel(model=self._list_store, filter=self._filter)
        self._selection_model = Gtk.NoSelection(model=self._filter_model)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_row)
        factory.connect("bind", self._bind_row)
        factory.connect("unbind", self._unbind_row)

        self._list_view = Gtk.ListView(
            model=self._selection_model,
            factory=factory,
        )
        self._list_view.set_show_separators(False)
        self._list_view.add_css_class("trawl-file-list")

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self._list_view)
        scroller.set_vexpand(True)
        self._browser_page = scroller

        self._content_stack = Gtk.Stack()
        self._content_stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self._content_stack.add_titled(self._browser_page, "browser", "Files")
        self._content_stack.add_titled(self._build_choice_page(), "choice", "Choose")
        self._content_stack.add_titled(self._build_confirm_page(), "confirm", "Confirm")
        self._toolbar.set_content(self._content_stack)

        # ------------------------------------------------------------- bottom
        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        bottom.set_margin_top(10)
        bottom.set_margin_bottom(12)
        bottom.set_margin_start(12)
        bottom.set_margin_end(12)
        bottom.add_css_class("trawl-bottom-bar")

        destination_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._destination_button = Gtk.Button()
        self._destination_button.add_css_class("nb-secondary")
        self._destination_button.set_halign(Gtk.Align.START)
        self._destination_button.connect("clicked", lambda *_: self._choose_destination())
        self._destination_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._destination_box.append(Gtk.Image(icon_name="folder-download-symbolic"))
        self._destination_label = Gtk.Label(label="")
        self._destination_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._destination_label.set_max_width_chars(52)
        self._destination_box.append(self._destination_label)
        self._destination_button.set_child(self._destination_box)
        destination_row.append(self._destination_button)

        self._space_label = Gtk.Label(label="")
        self._space_label.add_css_class("nb-tag-blue")
        self._space_label.set_hexpand(True)
        self._space_label.set_xalign(0)
        destination_row.append(self._space_label)
        bottom.append(destination_row)

        action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self._progress_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._progress_box.add_css_class("nb-progress")
        self._progress_box.set_hexpand(True)
        self._progress_bar = Gtk.ProgressBar()
        self._progress_bar.set_show_text(False)
        self._progress_label = Gtk.Label(label="")
        self._progress_label.set_xalign(0)
        self._progress_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._progress_label.add_css_class("caption")
        self._progress_label.add_css_class("dim-label")
        self._progress_box.append(self._progress_bar)
        self._progress_box.append(self._progress_label)
        self._progress_revealer = Gtk.Revealer()
        settings = Gtk.Settings.get_default()
        if settings is not None and not settings.get_property("gtk-enable-animations"):
            self._progress_revealer.set_transition_type(Gtk.RevealerTransitionType.NONE)
        else:
            self._progress_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self._progress_revealer.set_child(self._progress_box)
        self._progress_revealer.set_hexpand(True)
        self._progress_revealer.set_reveal_child(False)
        action_row.append(self._progress_revealer)

        self._open_button = Gtk.Button(label="Open folder")
        self._open_button.add_css_class("nb-secondary")
        self._open_button.set_visible(False)
        self._open_button.connect("clicked", lambda *_: self._open_destination())
        action_row.append(self._open_button)

        self._cancel_button = Gtk.Button(label="Cancel")
        self._cancel_button.add_css_class("nb-secondary")
        self._cancel_button.add_css_class("nb-red")
        self._cancel_button.set_visible(False)
        self._cancel_button.connect("clicked", lambda *_: self._cancel_download())
        action_row.append(self._cancel_button)

        self._download_all_button = Gtk.Button(label="Download all")
        self._download_all_button.add_css_class("nb-secondary")
        self._download_all_button.set_tooltip_text("Go to the download-all confirmation")
        self._download_all_button.connect("clicked", lambda *_: self._show_confirm())
        action_row.append(self._download_all_button)

        self._download_button = Gtk.Button(label="Download selected")
        self._download_button.add_css_class("nb-button")
        self._download_button.set_sensitive(False)
        self._download_button.connect("clicked", lambda *_: self._begin_download())
        action_row.append(self._download_button)
        bottom.append(action_row)

        self._bottom_bar = bottom
        self._toolbar.add_bottom_bar(self._bottom_bar)

        self._install_actions()
        self._set_destination(self._destination, remember=False)
        self._load_entries()
        self._update_selection_ui()
        self._set_mode(self._mode)

    # ------------------------------------------------------------ choice flow
    def _make_choice_card(
        self,
        title: str,
        description: str,
        info: str,
        style: str,
        callback,
    ) -> Gtk.Button:
        button = Gtk.Button()
        button.add_css_class("nb-choice-card")
        button.add_css_class(style)
        button.set_hexpand(True)
        button.set_vexpand(False)
        button.set_size_request(-1, 200)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        inner.set_halign(Gtk.Align.START)
        inner.set_valign(Gtk.Align.FILL)
        inner.set_vexpand(True)
        inner.set_margin_top(20)
        inner.set_margin_bottom(20)
        inner.set_margin_start(20)
        inner.set_margin_end(20)

        title_label = Gtk.Label(label=title, xalign=0)
        title_label.add_css_class("nb-choice-title")
        inner.append(title_label)

        description_label = Gtk.Label(
            label=description, xalign=0, ellipsize=Pango.EllipsizeMode.END
        )
        description_label.add_css_class("nb-choice-sub")
        inner.append(description_label)

        spacer = Gtk.Box()
        spacer.set_vexpand(True)
        inner.append(spacer)

        info_label = Gtk.Label(label=info, xalign=0)
        info_label.add_css_class("nb-choice-count")
        info_label.set_valign(Gtk.Align.END)
        inner.append(info_label)

        button.set_child(inner)
        button.connect("clicked", lambda *_: callback())
        return button

    def _build_choice_page(self) -> Gtk.Widget:
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        content.set_valign(Gtk.Align.START)
        content.set_vexpand(False)

        heading = Gtk.Label(label="How do you want to continue?", xalign=0)
        heading.add_css_class("nb-choice-title")
        heading.set_halign(Gtk.Align.START)
        content.append(heading)

        count = human_count(self._total_files, "file")
        cards_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        cards_box.set_homogeneous(True)
        cards_box.set_vexpand(False)
        all_card = self._make_choice_card(
            "Download all",
            "Get everything in this archive.",
            f"{human_size(self._total_size)} to download",
            "nb-choice-all",
            self.choose_all,
        )
        select_card = self._make_choice_card(
            "Select files",
            "Browse the list and tick what you want.",
            f"Choose from {count}",
            "nb-choice-select",
            self.choose_select,
        )
        cards_box.append(all_card)
        cards_box.append(select_card)

        cards = Adw.BreakpointBin()
        cards.set_child(cards_box)
        cards.set_vexpand(False)
        cards.set_halign(Gtk.Align.FILL)
        condition = Adw.BreakpointCondition.new_length(
            Adw.BreakpointConditionLengthType.MAX_WIDTH, 640, Adw.LengthUnit.PX
        )
        breakpoint = Adw.Breakpoint.new(condition)
        breakpoint.add_setter(cards_box, "orientation", Gtk.Orientation.VERTICAL)
        breakpoint.add_setter(all_card, "height-request", 160)
        breakpoint.add_setter(select_card, "height-request", 160)
        cards.add_breakpoint(breakpoint)
        content.append(cards)

        clamp = Adw.Clamp(maximum_size=720, tightening_threshold=640)
        clamp.set_child(content)
        clamp.set_margin_top(40)
        clamp.set_margin_bottom(28)
        clamp.set_margin_start(24)
        clamp.set_margin_end(24)
        clamp.set_valign(Gtk.Align.START)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.add_css_class("nb-canvas")
        scroller.set_child(clamp)
        return scroller

    def _build_confirm_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.add_css_class("nb-confirm-screen")

        title = Gtk.Label(label="Download everything?", xalign=0)
        title.add_css_class("nb-choice-title")
        box.append(title)

        self._confirm_summary = Gtk.Label(
            label=f"{human_count(self._total_files, 'file')} · {human_size(self._total_size)}",
            xalign=0,
        )
        self._confirm_summary.add_css_class("nb-choice-count")
        box.append(self._confirm_summary)

        destination_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        destination_box.append(Gtk.Label(label="Save to", xalign=0))
        self._confirm_destination = Gtk.Label(label="")
        self._confirm_destination.add_css_class("nb-tag-blue")
        self._confirm_destination.set_ellipsize(Pango.EllipsizeMode.END)
        destination_box.append(self._confirm_destination)
        box.append(destination_box)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        change = Gtk.Button(label="Change folder")
        change.add_css_class("nb-secondary")
        change.connect("clicked", lambda *_: self._choose_destination())
        actions.append(change)

        self._confirm_button = Gtk.Button(label="Download")
        self._confirm_button.add_css_class("nb-button")
        self._confirm_button.connect("clicked", lambda *_: self._confirm_download_all())
        actions.append(self._confirm_button)
        box.append(actions)

        clamp = Adw.Clamp(maximum_size=620, tightening_threshold=420)
        clamp.set_child(box)
        clamp.set_margin_top(28)
        clamp.set_margin_bottom(32)
        clamp.set_margin_start(24)
        clamp.set_margin_end(28)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.add_css_class("nb-canvas")
        scroller.set_child(clamp)
        return scroller

    def _update_confirm_page(self) -> None:
        if not hasattr(self, "_confirm_destination"):
            return
        self._confirm_destination.set_text(tilde_path(self._destination))
        if hasattr(self, "_confirm_button"):
            self._confirm_button.set_label(
                f"Download {human_count(self._total_files, 'file')} · "
                f"{human_size(self._total_size)}"
            )

    @property
    def mode(self) -> str:
        return self._mode

    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        self._content_stack.set_visible_child_name(mode)
        browser = mode == "browser"
        self._filter_bar.set_visible(browser)
        self._bottom_bar.set_visible(browser)
        self.sync_header()
        self._update_selection_ui()

    def sync_header(self) -> None:
        if self._choice_available and self._mode == "choice":
            self.window.set_back_visible(False)
            self.window.set_home_mode(True, self._close_to_home)
        else:
            self.window.set_home_mode(False)
            visible = self._choice_available and self._mode != "choice"
            self.window.set_back_visible(visible, self.go_back)

    def _close_to_home(self) -> None:
        self.window.close_session(self.session)

    def go_back(self) -> None:
        if self._choice_available and self._mode != "choice":
            self._set_mode("choice")

    def choose_all(self) -> None:
        self._settings.last_choice = "all"
        try:
            self._store.save()
        except OSError:
            pass
        self._show_confirm()

    def choose_select(self) -> None:
        self._settings.last_choice = "select"
        try:
            self._store.save()
        except OSError:
            pass
        self._set_mode("browser")

    def _show_confirm(self) -> None:
        if not self._choice_available or self._downloading:
            return
        self._update_confirm_page()
        self._set_mode("confirm")

    def _confirm_download_all(self) -> None:
        entries = sorted(
            (entry for entry in self.archive.files if entry.selectable),
            key=lambda entry: entry.path.casefold(),
        )
        if not entries:
            self.window.toast("There are no files to download")
            return
        self._set_mode("browser")
        self._begin_download(entries, mode="all")

    # --------------------------------------------------------------- actions
    def _install_actions(self) -> None:
        group = Gio.SimpleActionGroup()
        actions = {
            "reload": self._action_reload,
            "copy-link": self._action_copy_link,
            "open-destination": lambda *_: self._open_destination(),
            "close": lambda *_: self.window.close_session(self.session),
        }
        for name, callback in actions.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            group.add_action(action)
        self.insert_action_group("page", group)

    def _action_reload(self, *_args) -> None:
        if self._downloading:
            return
        if self.session.loading:
            return
        self._search.set_text("")
        self._selection_label.set_text("Re-reading index…")
        self._set_busy(True)
        threading.Thread(target=self._reload_worker, name="trawl-reload", daemon=True).start()

    def _reload_worker(self) -> None:
        try:
            archive = self.archive
            archive.entries.clear()
            archive.warnings.clear()

            def progress(message: str) -> None:
                GLib.idle_add(self._set_reload_status, message)

            archive.read_index(progress)
        except Exception as error:
            GLib.idle_add(self._reload_failed, str(error) or error.__class__.__name__)
            return
        GLib.idle_add(self._reload_finished)

    def _set_reload_status(self, message: str) -> bool:
        if not self._alive:
            return False
        self._selection_label.set_text(message)
        return False

    def _reload_failed(self, message: str) -> bool:
        if not self._alive:
            return False
        self._set_busy(False)
        self._update_selection_ui()
        self.window.toast(f"Could not reload the index: {message}")
        return False

    def _reload_finished(self) -> bool:
        if not self._alive:
            return False
        self._selected.clear()
        self._load_entries()
        self._set_busy(False)
        self._update_selection_ui()
        self.window.toast("Archive index refreshed")
        return False

    def _action_copy_link(self, *_args) -> None:
        self.get_clipboard().set(self.session.url)
        self.window.toast("Link copied")

    def _set_busy(self, busy: bool) -> None:
        for widget in (
            self._search,
            self._select_all_button,
            self._clear_button,
            self._destination_button,
            self._list_view,
        ):
            widget.set_sensitive(not busy)
        if busy:
            self._download_button.set_sensitive(False)
        else:
            self._update_selection_ui()

    # ------------------------------------------------------------------- list
    def _load_entries(self) -> None:
        entries = sorted(self.archive.files, key=lambda item: item.path.casefold())
        self._selected.clear()
        self._bulk_select = True
        objects: list[EntryObject] = []
        auto_select = len(entries) <= 5
        for index, entry in enumerate(entries):
            obj = EntryObject(entry, index)
            obj.connect("notify::selected", self._on_selection_changed)
            objects.append(obj)
            if auto_select and entry.selectable:
                obj.selected = True
        self._bulk_select = False
        self._objects = objects
        self._list_store.splice(0, self._list_store.get_n_items(), self._objects)
        self._filter.changed(Gtk.FilterChange.DIFFERENT)
        if not self._objects:
            self._selection_label.set_text("This archive has no files")
        self._update_space_label()

    def _matches_filter(self, obj, _data) -> bool:
        query = self._search.get_text().strip().casefold()
        if not query:
            return True
        entry = obj.entry
        return query in entry.path.casefold()

    def _refilter(self) -> None:
        self._filter.changed(Gtk.FilterChange.DIFFERENT)
        self._update_selection_ui()

    def _setup_row(self, _factory, list_item) -> None:
        check = Gtk.CheckButton()
        check.set_hexpand(True)
        check.add_css_class("nb-file-row")

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(10)
        box.set_margin_end(14)

        icon = Gtk.Image()
        icon.set_pixel_size(20)
        icon.add_css_class("trawl-file-icon")
        box.append(icon)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        text_box.set_hexpand(True)
        name = Gtk.Label(xalign=0)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.add_css_class("trawl-file-name")
        folder = Gtk.Label(xalign=0)
        folder.set_ellipsize(Pango.EllipsizeMode.END)
        folder.add_css_class("trawl-file-folder")
        text_box.append(name)
        text_box.append(folder)
        box.append(text_box)

        warning = Gtk.Image(icon_name="dialog-warning-symbolic")
        warning.add_css_class("nb-warning")
        name_icon(warning, "This file cannot be downloaded")
        warning.set_visible(False)
        box.append(warning)

        size = Gtk.Label(xalign=1)
        size.add_css_class("trawl-file-size")
        box.append(size)

        check.set_child(box)
        list_item.set_child(check)
        check._trawl_binding = None
        check._trawl_widgets = (icon, name, folder, warning, size)

    def _bind_row(self, _factory, list_item) -> None:
        obj: EntryObject = list_item.get_item()
        check: Gtk.CheckButton = list_item.get_child()
        icon, name, folder, warning, size = check._trawl_widgets
        entry = obj.entry
        icon.set_from_icon_name(_icon_for(entry.path, entry.is_dir))
        name.set_text(entry.name or entry.path)
        folder.set_text(entry.folder)
        folder.set_visible(bool(entry.folder))
        size.set_text(human_size(entry.size))
        unsupported = entry.unsupported
        warning.set_visible(bool(unsupported))
        warning.set_tooltip_text(unsupported or "")
        name_icon(check, entry.path + (" — " + unsupported if unsupported else ""))
        check.set_tooltip_text(entry.path if not unsupported else f"{entry.path}\n{unsupported}")
        check.set_sensitive(entry.selectable)
        if check._trawl_binding is not None:
            check._trawl_binding.unbind()
        check.set_active(obj.selected)
        check._trawl_binding = check.bind_property(
            "active",
            obj,
            "selected",
            GObject.BindingFlags.BIDIRECTIONAL,
        )

    def _unbind_row(self, _factory, list_item) -> None:
        check: Gtk.CheckButton = list_item.get_child()
        if check._trawl_binding is not None:
            check._trawl_binding.unbind()
            check._trawl_binding = None

    def _on_selection_changed(self, obj: EntryObject, _pspec) -> None:
        if obj.selected and not obj.entry.selectable:
            obj.selected = False
            return
        if obj.selected:
            self._selected.add(obj)
        else:
            self._selected.discard(obj)
        if not getattr(self, "_bulk_select", False):
            self._update_selection_ui()

    def _visible_objects(self) -> list[EntryObject]:
        return [
            self._filter_model.get_item(index)
            for index in range(self._filter_model.get_n_items())
        ]

    def _select_all(self) -> None:
        self._bulk_select = True
        for obj in self._visible_objects():
            if obj.entry.selectable:
                obj.selected = True
        self._bulk_select = False
        self._update_selection_ui()

    def _clear_selection(self) -> None:
        if not self._selected:
            return
        self._bulk_select = True
        for obj in list(self._selected):
            obj.selected = False
        self._bulk_select = False
        self._update_selection_ui()

    def _update_selection_ui(self) -> None:
        selected = [obj for obj in self._selected if obj.entry.selectable]
        count = len(selected)
        total_size = sum(obj.entry.size for obj in selected)
        total_files = len(self._objects)
        if total_files:
            if count:
                self._selection_label.set_text(
                    f"{human_count(count, 'file')} · {human_size(total_size)} selected"
                )
            else:
                self._selection_label.set_text(f"{human_count(total_files, 'file')} available")
        self._download_button.set_sensitive(count > 0 and not self._downloading)
        self._download_button.set_label(
            f"Download selected ({count})" if count else "Download selected"
        )
        self._clear_button.set_sensitive(bool(self._selected) and not self._downloading)
        self._select_all_button.set_sensitive(
            total_files > 0 and not self._downloading
        )
        if hasattr(self, "_download_all_button"):
            self._download_all_button.set_sensitive(
                self._total_files > 0 and self._choice_available and not self._downloading
            )
        self._update_space_label(total_size)

    # ------------------------------------------------------------ destination
    def _default_destination(self, archive_name: str) -> Path:
        base = self._settings.effective_download_dir()
        return base / archive_folder_name(archive_name)

    def _set_destination(self, path: Path, remember: bool = True) -> None:
        self._destination = Path(path)
        self._destination_label.set_text(tilde_path(self._destination))
        self._destination_button.set_tooltip_text(str(self._destination))
        self._update_space_label()
        self._update_confirm_page()
        if remember:
            try:
                self._settings.download_dir = str(self._destination.parent)
                self._store.save()
            except OSError:
                pass

    def _choose_destination(self) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose where to save the files")
        try:
            dialog.set_initial_folder(Gio.File.new_for_path(str(_nearest_existing(self._destination))))
        except Exception:
            pass
        dialog.select_folder(self.get_root(), None, self._destination_chosen)

    def _destination_chosen(self, dialog, result) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except Exception:
            return
        if folder is None:
            return
        path = folder.get_path()
        if path:
            self._set_destination(Path(path))
            self._update_selection_ui()

    def _update_space_label(self, selected_size: int | None = None) -> None:
        if selected_size is None:
            selected_size = sum(obj.entry.size for obj in self._selected)
        available = free_space(_nearest_existing(self._destination))
        if available is None:
            self._space_label.set_text("Destination folder")
            return
        text = f"{human_size(available)} free"
        if selected_size > available:
            text += " · not enough for the selection"
            self._space_label.add_css_class("warning")
        else:
            self._space_label.remove_css_class("warning")
        self._space_label.set_text(text)

    # --------------------------------------------------------------- download
    def _begin_download(
        self, entries: list[Entry] | None = None, mode: str = "selected"
    ) -> None:
        if self._downloading or self._job is not None:
            return
        if entries is None:
            entries = sorted(
                (obj.entry for obj in self._selected if obj.entry.selectable),
                key=lambda entry: entry.path.casefold(),
            )
        selected = entries
        if not selected:
            self.window.toast("Select at least one file first")
            return
        needed = sum(entry.size for entry in selected)
        available = free_space(_nearest_existing(self._destination))
        message = describe_missing_space(needed, available)
        if message:
            TrawlAlert(
                self.get_root(),
                "Not enough free space",
                message,
                [
                    ("cancel", "Cancel", "nb-secondary"),
                    ("download", "Download anyway", "nb-green"),
                ],
                "cancel",
                lambda response, s=selected, m=mode: self._space_response(response, s, m),
            ).present()
            return
        self._start_download(selected, mode)

    def _space_response(self, response: str, selected, mode: str = "selected") -> None:
        if response == "download":
            self._start_download(selected, mode)

    def _start_download(self, entries: list[Entry], mode: str = "selected") -> None:
        if self._downloading:
            return
        self._download_mode = mode
        self._download_started_at = time.monotonic()
        self._last_progress_log = self._download_started_at
        options = DownloadOptions(
            threads=self._settings.threads,
            skip_existing=self._settings.skip_existing,
            overwrite=self._settings.overwrite,
            resume=self._settings.resume,
            verify=self._settings.verify,
            preserve_mtime=self._settings.preserve_mtime,
        )
        self._downloading = True
        self._job = DownloadJob(
            archive=self.archive,
            entries=entries,
            destination=self._destination,
            options=options,
            on_update=self._job_update,
            on_finished=self._job_finished,
        )
        self._progress_revealer.set_reveal_child(True)
        self._progress_bar.set_fraction(0.0)
        self._progress_label.set_text("Preparing…")
        self._download_button.set_visible(False)
        self._cancel_button.set_visible(True)
        self._cancel_button.set_sensitive(True)
        self._cancel_button.set_label("Cancel")
        self._open_button.set_visible(True)
        self._set_busy(True)
        self._job.start()

    def _job_update(self, progress) -> None:
        now = time.monotonic()
        if now - self._last_progress_log >= 300:
            self._last_progress_log = now
            self.launch_log.record_progress(
                bytes_done=progress.done_bytes,
                files_done=progress.done_files,
                files_failed=progress.failed_files,
            )
        GLib.idle_add(self._apply_progress, progress)

    def _job_finished(self, progress) -> None:
        GLib.idle_add(self._apply_finished, progress)

    def _apply_progress(self, progress) -> bool:
        if not self._alive:
            return False
        total = progress.total_bytes
        if total > 0:
            fraction = min(1.0, progress.done_bytes / total)
        elif progress.total_files:
            fraction = progress.done_files / progress.total_files
        else:
            fraction = 1.0
        self._progress_bar.set_fraction(fraction)
        if progress.status == "running":
            remaining = max(0, total - progress.done_bytes)
            eta = human_eta(remaining / progress.speed) if progress.speed > 0 else ""
            parts = [
                f"{human_size(progress.done_bytes)} of {human_size(total)}",
                human_speed(progress.speed) if progress.speed > 0 else "",
                eta,
            ]
            self._progress_label.set_text(" · ".join(part for part in parts if part))
        elif progress.status == "queued":
            self._progress_label.set_text("Preparing…")
        return False

    def _apply_finished(self, progress) -> bool:
        if not self._alive:
            return False
        self._downloading = False
        self._job = None
        failed = progress.failed_files
        if progress.status == "cancelled":
            result = "cancelled"
        elif failed:
            result = "partial"
        elif progress.status == "failed":
            result = "failed"
        else:
            result = "complete"

        self.launch_log.record_download(
            files=progress.total_files,
            failed=failed,
            skipped=progress.skipped_files,
            bytes_written=progress.done_bytes,
            mode=self._download_mode,
            selected_files=len(self._selected),
            total_files=self._total_files,
            duration_seconds=time.monotonic() - self._download_started_at,
            result=result,
        )
        if failed:
            self.launch_log.record_error("download_failed", "DownloadError")

        self._cancel_button.set_visible(False)
        self._download_button.set_visible(True)
        self._progress_bar.set_fraction(1.0)

        focused = self._is_window_focused()

        if result == "cancelled":
            self._progress_label.set_text("Cancelled — partial downloads are kept")
        elif result == "partial":
            self._progress_label.set_text(
                f"Finished with {human_count(failed, 'failed file')} · "
                f"{human_size(progress.done_bytes)} written"
            )
        elif result == "failed":
            self._progress_label.set_text("Download failed")
        else:
            skipped = progress.skipped_files
            suffix = f" · {human_count(skipped, 'file')} skipped" if skipped else ""
            self._progress_label.set_text(
                f"Finished · {human_size(progress.done_bytes)}{suffix}"
            )

        # The popup covers the focused case; a toast would be a duplicate.
        if not focused:
            if result == "cancelled":
                self.window.toast("Download cancelled")
            elif result == "partial":
                self.window.toast(f"{human_count(failed, 'file')} failed to download")
            elif result == "failed":
                self.window.toast("Download failed")
            else:
                self.window.toast("Download complete")

        # Never notify or play a sound for a download the user cancelled.
        if result != "cancelled":
            self._finish_notification(progress, result, focused)
        self._set_busy(False)
        self._update_selection_ui()
        self._update_space_label()
        return False

    def _is_window_focused(self) -> bool:
        root = self.get_root()
        try:
            return bool(root is not None and root.is_active())
        except Exception:
            return True

    def _finish_notification(self, progress, result: str, focused: bool) -> None:
        failed = progress.failed_files
        if result == "partial":
            body = f"Finished with {human_count(failed, 'failed file')}"
        elif result == "failed":
            body = "Download failed"
        else:
            body = (
                f"Download finished · {human_count(progress.total_files, 'file')} · "
                f"{human_size(progress.done_bytes)}"
            )

        responses = [("close", "Close", "nb-secondary")]
        if result == "partial" and failed:
            responses.append(("failed", "Show failed files", "nb-secondary"))
        responses.append(("open", "Open folder", "nb-button nb-pink"))

        def on_response(response: str) -> None:
            if response == "open":
                self._open_destination()
            elif response == "failed":
                self._show_failures(progress)

        TrawlAlert(
            self.get_root(),
            "Download finished",
            body,
            responses,
            "close",
            on_response,
        ).present()

        play_sound(
            "complete" if result == "complete" else "dialog-error",
            self._settings.play_sound,
        )
        if not focused:
            send_notification(
                self.window.get_application(),
                body,
                str(self._destination),
                self._settings.notify_finish,
            )

    def _show_failures(self, progress) -> None:
        failures = [item for item in progress.files if item.status == "failed"]
        if not failures:
            return
        lines = []
        for item in failures[:12]:
            lines.append(f"• {item.entry.path}: {item.error or 'download failed'}")
        if len(failures) > 12:
            lines.append(f"…and {len(failures) - 12} more")
        TrawlAlert(
            self.get_root(),
            f"{human_count(len(failures), 'file')} failed",
            "\n".join(lines),
            [("close", "Close", "nb-secondary")],
            "close",
            lambda *_args: None,
        ).present()

    def _cancel_download(self) -> None:
        if self._job is not None:
            self._cancel_button.set_sensitive(False)
            self._cancel_button.set_label("Cancelling…")
            self._job.cancel()

    def _open_destination(self) -> None:
        try:
            self._destination.mkdir(parents=True, exist_ok=True)
            uri = Gio.File.new_for_path(str(self._destination)).get_uri()
            Gtk.UriLauncher.new(uri=uri).launch(self.get_root(), None, None)
        except Exception as error:
            self.window.toast(f"Could not open the folder: {error}")

    # ------------------------------------------------------------------ misc
    @property
    def destination(self) -> Path:
        return self._destination

    @property
    def is_downloading(self) -> bool:
        return self._downloading

    def shutdown(self) -> None:
        self._alive = False
        if self._job is not None:
            self._job.cancel()
            self._job = None
        try:
            self.archive.close()
        except Exception:
            pass
