from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from .. import APP_NAME
from ..archives import open_archive
from ..errors import error_code, friendly_message
from ..archives.base import Archive, ArchiveError
from ..archives.resolvers import (
    UnsupportedHost,
    describe_host,
    name_from_url,
    resolve_candidates,
    scrape_archive_url,
)
from ..net import (
    HTTPRangeReader,
    INVALID_LINK_MESSAGE,
    RangeUnsupported,
    RemoteInfo,
    TrawlNetError,
    fetch_page,
    validate_link,
)
from ..settings import HistoryItem, SettingsStore
from ..util import (
    human_count,
    human_relative_time,
    human_size,
    parse_header_lines,
    sanitize_filename,
)
from . import name_icon
from .add_dialog import AddLinkDialog
from .alert import TrawlAlert
from .archive_page import ArchivePage
from .settings_dialog import SettingsDialog

log = logging.getLogger(__name__)


def _truncate(text: str, limit: int = 40) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


class LoadCancelled(Exception):
    pass


@dataclass
class Session:
    url: str
    filename: str
    host: str
    headers_text: str = ""
    insecure: bool = False
    slow: bool = False
    reader: HTTPRangeReader | None = None
    info: RemoteInfo | None = None
    archive: Archive | None = None
    page: ArchivePage | None = None
    content_page: Gtk.Widget | None = None
    loading: bool = True
    error: str = ""
    cancel: threading.Event = field(default_factory=threading.Event)
    seen: set[str] = field(default_factory=set)
    status_page: Adw.StatusPage | None = None
    status_label: Gtk.Label | None = None
    spinner: Gtk.Spinner | None = None


class MainWindow(Adw.ApplicationWindow):
    """Single-pane window: one header, one content area, no sidebar column."""

    def __init__(self, app, store: SettingsStore):
        super().__init__(application=app)
        self.add_css_class("nb-root")
        self.store = store
        self.settings = store.settings
        self.launch_log = app.launch_log
        self.sessions: list[Session] = []
        self._recent_rows: list[Adw.ActionRow] = []
        self._updating_dropdown = False

        self.set_title(APP_NAME)
        self.set_default_size(self.settings.window_width, self.settings.window_height)
        self.set_size_request(640, 480)
        if self.settings.maximized:
            self.maximize()

        # ---------------------------------------------------------- main shell
        self._toolbar = Adw.ToolbarView()
        self._header = Adw.HeaderBar()
        self._build_header()
        self._toolbar.add_top_bar(self._header)

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self._stack.set_hexpand(True)
        self._stack.set_vexpand(True)
        self._toolbar.set_content(self._stack)

        self._recent_group = self._build_recent_group()
        self._empty_page = self._build_empty_page()
        self._stack.add_titled(self._empty_page, "empty", APP_NAME)
        self._stack.set_visible_child(self._empty_page)

        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_child(self._toolbar)
        self.set_content(self._toast_overlay)
        self.connect("close-request", self._on_close_request)
        self._keys = Gtk.EventControllerKey.new()
        self._keys.connect("key-pressed", self._on_key_pressed)
        self.add_controller(self._keys)
        self._refresh_recent()
        self._update_header()

    # ----------------------------------------------------------------- header
    def _build_header(self) -> None:
        # No leading decoration: the window icon would otherwise appear as a
        # tiny logo at the far left of the header bar.
        self._header.set_decoration_layout(":minimize,maximize,close")

        self._back_button = Gtk.Button(icon_name="go-previous-symbolic")
        self._back_button.add_css_class("nb-icon-button")
        name_icon(self._back_button, "Back to the choice screen (Esc)")
        self._back_button.set_visible(False)
        self._back_callback = None
        self._back_button.connect("clicked", lambda *_: self._activate_back())
        self._header.pack_start(self._back_button)

        self._home_button = Gtk.Button(icon_name="go-home-symbolic")
        self._home_button.add_css_class("nb-icon-button")
        name_icon(self._home_button, "Back to home (Esc)")
        self._home_button.set_visible(False)
        self._home_callback = None
        self._home_button.connect(
            "clicked", lambda *_: self._activate_home()
        )
        self._header.pack_start(self._home_button)

        self._add_button = Gtk.Button(icon_name="list-add-symbolic")
        self._add_button.add_css_class("nb-icon-button")
        name_icon(self._add_button, "Open a remote archive (Ctrl+L)")
        self._add_button.connect("clicked", lambda *_: self.add_link())
        self._header.pack_start(self._add_button)

        self._window_title = Adw.WindowTitle(title=APP_NAME, subtitle="Remote archives")
        self._header.set_title_widget(self._window_title)

        self._session_store = Gtk.StringList.new([])
        self._session_dropdown = Gtk.DropDown(model=self._session_store)
        self._session_dropdown.set_tooltip_text("Switch between open archives")
        name_icon(self._session_dropdown, "Open archives")
        self._session_dropdown.set_visible(False)
        self._session_dropdown.connect("notify::selected", self._on_session_selected)
        self._header.pack_end(self._session_dropdown)

        menu = Gio.Menu()
        menu.append("Reload index", "page.reload")
        menu.append("Copy link", "page.copy-link")
        menu.append("Open destination", "page.open-destination")
        menu.append("Close archive", "page.close")
        self._menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic")
        self._menu_button.add_css_class("nb-icon-button")
        self._menu_button.set_menu_model(menu)
        name_icon(self._menu_button, "Archive menu")
        self._menu_button.set_visible(False)
        self._header.pack_end(self._menu_button)

        group = Gio.SimpleActionGroup()
        actions = {
            "reload": lambda *_: self._active_page_action("_action_reload"),
            "copy-link": lambda *_: self._active_page_action("_action_copy_link"),
            "open-destination": lambda *_: self._active_page_action("_open_destination"),
            "close": lambda *_: self._close_active_session(),
        }
        for name, callback in actions.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            group.add_action(action)
        self.insert_action_group("page", group)

    # ------------------------------------------------------------- empty page
    def _build_recent_group(self) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card.add_css_class("nb-recent-card")
        card.set_visible(False)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        sticker = Gtk.Label(label="Recent")
        sticker.add_css_class("nb-tag")
        sticker.set_xalign(0)
        header.append(sticker)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        header.append(spacer)

        clear_button = Gtk.Button(label="Clear")
        clear_button.add_css_class("nb-secondary")
        clear_button.add_css_class("nb-small")
        clear_button.set_valign(Gtk.Align.CENTER)
        clear_button.set_tooltip_text("Clear recent links")
        clear_button.connect("clicked", lambda *_: self._clear_recent())
        header.append(clear_button)
        card.append(header)

        self._recent_rows_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8
        )
        card.append(self._recent_rows_box)
        self._recent_card = card
        return card

    def _build_empty_page(self) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        card.add_css_class("nb-hero")

        title = Gtk.Label(
            label="Pull files out of remote archives",
            xalign=0,
        )
        title.add_css_class("nb-hero-title")
        card.append(title)

        body = Gtk.Label(
            label=(
                "Paste a ZIP, RAR or ISO link and download only the files you need."
            ),
            xalign=0,
            ellipsize=Pango.EllipsizeMode.END,
        )
        body.add_css_class("nb-hero-body")
        card.append(body)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        actions.set_halign(Gtk.Align.START)
        open_button = Gtk.Button(label="Open a link")
        open_button.add_css_class("nb-cta")
        open_button.set_halign(Gtk.Align.START)
        open_button.connect("clicked", lambda *_: self.add_link())
        actions.append(open_button)

        chips = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        chips.add_css_class("nb-chip-group")
        chips.set_halign(Gtk.Align.START)
        for text, color in (
            ("ZIP", "yellow"),
            ("ZIP64", "pink"),
            ("RAR", "blue"),
            ("ISO", "lime"),
        ):
            chip = Gtk.Label(label=text)
            chip.add_css_class("nb-chip")
            chip.add_css_class(f"nb-chip-{color}")
            chips.append(chip)
        actions.append(chips)
        card.append(actions)

        steps = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        steps.add_css_class("nb-steps-compact")
        steps.set_homogeneous(True)
        for number, text in (
            ("1", "Paste a link"),
            ("2", "Pick the files"),
            ("3", "Download just those"),
        ):
            step = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            step.add_css_class("nb-step")
            step.add_css_class(f"nb-step-{number}")
            num = Gtk.Label(label=number, xalign=0.5)
            num.add_css_class("nb-step-num")
            step.append(num)
            label = Gtk.Label(label=text, xalign=0)
            label.add_css_class("nb-step-text")
            step.append(label)
            steps.append(step)
        card.append(steps)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        content.append(card)
        content.append(self._recent_group)

        clamp = Adw.Clamp(maximum_size=720, tightening_threshold=640)
        clamp.set_child(content)
        clamp.set_margin_top(24)
        clamp.set_margin_bottom(24)
        clamp.set_margin_start(24)
        clamp.set_margin_end(24)
        clamp.set_valign(Gtk.Align.CENTER)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.add_css_class("nb-canvas")
        scroller.set_child(clamp)
        return scroller

    # ------------------------------------------------------------------ pages
    def _set_content_page(self, widget: Gtk.Widget, name: str) -> None:
        if widget.get_parent() is not self._stack:
            self._stack.add_titled(widget, name, name)
        self._stack.set_visible_child(widget)
        self._update_header()

    def _show_empty(self) -> None:
        self._stack.set_visible_child(self._empty_page)
        self.set_focus(None)
        self._update_header()

    def _active_session(self) -> Session | None:
        visible = self._stack.get_visible_child()
        for session in self.sessions:
            if session.content_page is visible:
                return session
        return None

    def _active_page(self) -> ArchivePage | None:
        session = self._active_session()
        return session.page if session is not None else None

    def _active_page_action(self, method: str) -> None:
        page = self._active_page()
        if page is not None:
            getattr(page, method)()

    def _close_active_session(self) -> None:
        session = self._active_session()
        if session is not None:
            self.close_session(session)

    # ---------------------------------------------------------- sessionchooser
    def _refresh_session_dropdown(self) -> None:
        self._updating_dropdown = True
        names = [session.filename for session in self.sessions]
        self._session_store.splice(0, self._session_store.get_n_items(), [])
        for name in names:
            self._session_store.append(name)
        self._session_dropdown.set_visible(len(self.sessions) > 1)
        active = self._active_session()
        if active is not None and active in self.sessions:
            self._session_dropdown.set_selected(self.sessions.index(active))
        else:
            self._session_dropdown.set_selected(Gtk.INVALID_LIST_POSITION)
        self._updating_dropdown = False

    def _on_session_selected(self, _dropdown, _pspec) -> None:
        if self._updating_dropdown:
            return
        index = self._session_dropdown.get_selected()
        if index < len(self.sessions):
            self.show_session(self.sessions[index])

    def set_home_mode(self, active: bool, callback=None) -> None:
        """On the choice screen the '+' becomes a Back-to-home button."""
        self._home_callback = callback
        self._add_button.set_visible(not active and bool(self.sessions))
        self._home_button.set_visible(active and callback is not None)
        if not active:
            self._back_callback = None
            self._back_button.set_visible(False)

    def set_back_visible(self, visible: bool, callback=None) -> None:
        self._back_callback = callback
        self._back_button.set_visible(bool(visible) and callback is not None)

    def _activate_back(self) -> None:
        if self._back_callback is not None:
            self._back_callback()

    def _activate_home(self) -> None:
        if self._home_callback is not None:
            self._home_callback()

    def _on_key_pressed(self, _controller, keyval: int, _keycode: int, _state) -> bool:
        page = self._active_page()
        if page is None:
            return False
        focus = self.get_focus()
        if isinstance(focus, (Gtk.Entry, Gtk.TextView)):
            return False
        if keyval == Gdk.KEY_Escape:
            if page.mode == "choice":
                page._close_to_home()
            else:
                page.go_back()
            return True
        if page.mode == "choice":
            if keyval == Gdk.KEY_1:
                page.choose_all()
                return True
            if keyval == Gdk.KEY_2:
                page.choose_select()
                return True
        return False

    def _update_header(self) -> None:
        session = self._active_session()
        # The empty screen has its own big CTA; the header "+" appears only
        # once an archive is open (including while it is loading).
        self._add_button.set_visible(bool(self.sessions))
        if session is None:
            self._window_title.set_title(_truncate(APP_NAME))
            self._window_title.set_subtitle("Remote archives")
            self._menu_button.set_visible(False)
        elif session.page is not None and session.info is not None:
            self._window_title.set_title(_truncate(session.filename))
            self._window_title.set_subtitle(
                f"{session.archive.kind} · {human_size(session.info.size)} · "
                f"{human_count(len(session.archive.files), 'file')}"
            )
            self._menu_button.set_visible(True)
        else:
            # Loading/error pages show only the host, never a long URL.
            self._window_title.set_title(
                _truncate(session.host if session.loading else (session.error or "Opening archive"))
            )
            self._window_title.set_subtitle(
                "Connecting…" if session.loading else "Could not open"
            )
            self._menu_button.set_visible(False)
        if session is not None and session.page is not None:
            session.page.sync_header()
        else:
            self.set_back_visible(False)
        self._refresh_session_dropdown()

    # ------------------------------------------------------------------ recent
    def _refresh_recent(self) -> None:
        while child := self._recent_rows_box.get_first_child():
            self._recent_rows_box.remove(child)
        self._recent_rows.clear()
        history = self.store.settings.history[:8]
        for item in history:
            subtitle_parts = []
            if item.kind:
                subtitle_parts.append(item.kind)
            if item.size:
                subtitle_parts.append(human_size(item.size))
            if item.opened_at:
                subtitle_parts.append(human_relative_time(item.opened_at))

            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            row.add_css_class("nb-recent-row")
            row.append(Gtk.Image(icon_name="package-x-generic-symbolic"))

            text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            text_box.set_hexpand(True)
            title = Gtk.Label(label=item.filename or item.url, xalign=0)
            title.add_css_class("nb-recent-title")
            title.set_ellipsize(Pango.EllipsizeMode.END)
            subtitle = Gtk.Label(
                label=" · ".join(subtitle_parts) or describe_host(item.url), xalign=0
            )
            subtitle.add_css_class("nb-recent-sub")
            text_box.append(title)
            text_box.append(subtitle)
            row.append(text_box)

            remove = Gtk.Button(icon_name="window-close-symbolic")
            remove.add_css_class("nb-icon-button")
            remove.add_css_class("nb-small")
            name_icon(remove, "Remove from recent links")
            remove.connect(
                "clicked",
                lambda *_url, url=item.url: self._forget_recent(url),
            )
            row.append(remove)

            click = Gtk.GestureClick.new()
            click.connect("pressed", lambda *_args, url=item.url: self.open_url(url))
            row.add_controller(click)
            self._recent_rows_box.append(row)
            self._recent_rows.append(row)
        self._recent_card.set_visible(bool(history))

    def _forget_recent(self, url: str) -> None:
        try:
            self.store.forget(url)
        except OSError:
            pass
        self._refresh_recent()

    def _clear_recent(self) -> None:
        self.store.clear_history()
        self._refresh_recent()
        self.toast("Recent links cleared")

    # ------------------------------------------------------------------ open
    def add_link(self) -> None:
        dialog = AddLinkDialog(self, self.settings, self._new_link_submitted)
        dialog.present(self)

    def _new_link_submitted(
        self, url: str, headers_text: str, insecure: bool, slow: bool
    ) -> None:
        self.settings.headers = headers_text
        self.settings.insecure = insecure
        self.settings.slow_mode = slow
        try:
            self.store.save()
        except OSError:
            pass
        self.open_url(url, headers_text=headers_text, insecure=insecure, slow=slow)

    def open_url(
        self,
        url: str,
        headers_text: str | None = None,
        insecure: bool | None = None,
        slow: bool | None = None,
    ) -> None:
        url, message = validate_link(url)
        if not url:
            self.launch_log.record_error("invalid_url", "ValueError")
            self.toast(message or INVALID_LINK_MESSAGE)
            return

        for existing in self.sessions:
            if existing.url == url:
                self.show_session(existing)
                if existing.error:
                    self._retry_session(existing)
                else:
                    self.toast("That archive is already open")
                return

        self.launch_log.record_url(url)
        filename = sanitize_filename(name_from_url(url), "archive")
        session = Session(
            url=url,
            filename=filename,
            host=describe_host(url),
            headers_text=self.settings.headers if headers_text is None else headers_text,
            insecure=self.settings.insecure if insecure is None else insecure,
            slow=self.settings.slow_mode if slow is None else slow,
        )
        self.sessions.append(session)
        self._refresh_session_dropdown()
        self._start_loading(session)

    def _start_loading(self, session: Session) -> None:
        session.loading = True
        session.error = ""
        session.cancel = threading.Event()
        session.seen.clear()

        status = Adw.StatusPage(title="Opening archive")
        status.add_css_class("nb-loading")

        spinner = Gtk.Spinner()
        spinner.set_size_request(24, 24)
        spinner.add_css_class("nb-spinner")
        spinner.start()

        status_label = Gtk.Label(
            label=f"Connecting to {session.host or 'the server'}…",
            wrap=True,
            xalign=0,
        )
        status_label.add_css_class("nb-loading-label")

        status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        status_row.set_halign(Gtk.Align.CENTER)
        status_row.append(spinner)
        status_row.append(status_label)

        cancel = Gtk.Button(label="Cancel")
        cancel.add_css_class("nb-secondary")
        cancel.add_css_class("nb-cancel")
        cancel.connect("clicked", lambda *_: self.close_session(session))

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_halign(Gtk.Align.CENTER)
        box.append(status_row)
        box.append(cancel)
        status.set_child(box)
        session.status_page = status
        session.status_label = status_label
        session.spinner = spinner
        session.content_page = status
        self._set_content_page(status, f"loading-{id(session)}")
        # Do not show a focus ring on the Cancel button when the page opens.
        self.set_focus(None)

        threading.Thread(
            target=self._load_worker,
            args=(session,),
            name=f"trawl-open-{session.host}",
            daemon=True,
        ).start()

    def show_session(self, session: Session) -> None:
        if session in self.sessions and session.content_page is not None:
            self._set_content_page(
                session.content_page,
                f"session-{id(session)}",
            )

    def _retry_session(self, session: Session) -> None:
        if session.loading:
            return
        self._start_loading(session)

    # -------------------------------------------------------------- loading
    def _load_worker(self, session: Session) -> None:
        try:
            reader, info, archive = self._load_archive(session)
        except LoadCancelled:
            GLib.idle_add(self._discard_session, session)
            return
        except Exception as error:
            log.info("Could not open %s: %s", session.url, error, exc_info=True)
            GLib.idle_add(self._load_failed, session, error)
            return
        GLib.idle_add(self._load_succeeded, session, reader, info, archive)

    def _load_archive(self, session: Session):
        headers = parse_header_lines(session.headers_text)
        queue = deque(resolve_candidates(session.url))
        hops = 0
        last_error: Exception | None = None

        while queue:
            if session.cancel.is_set():
                raise LoadCancelled()
            candidate = queue.popleft()
            if candidate in session.seen:
                continue
            session.seen.add(candidate)
            self._loading_message(session, f"Connecting to {describe_host(candidate)}")
            reader = HTTPRangeReader(
                candidate,
                headers,
                insecure=session.insecure,
                rate_limit=session.slow,
            )
            session.reader = reader
            try:
                try:
                    info = reader.probe()
                except RangeUnsupported as error:
                    info = error.info
                    if info.is_html and hops < 2:
                        target = self._scrape(candidate, headers, session)
                        if target is not None:
                            try:
                                reader.close()
                            except Exception:
                                pass
                            if session.reader is reader:
                                session.reader = None
                            queue.appendleft(target)
                            hops += 1
                            continue
                    raise

                if info.is_html:
                    if hops >= 2:
                        raise TrawlNetError(
                            "That link leads to another web page, not an archive."
                        )
                    target = self._scrape(candidate, headers, session)
                    if target is None:
                        raise TrawlNetError(
                            "Trawl could not find a direct archive link on that page. "
                            "Try the site's download button and paste the direct link."
                        )
                    queue.appendleft(target)
                    hops += 1
                    continue

                self._loading_message(session, "Identifying the archive format")
                archive = open_archive(reader, info)
                self._loading_message(session, f"Reading the {archive.kind} index")
                archive.read_index(
                    lambda message: self._loading_message(session, message)
                )
                if session.cancel.is_set():
                    raise LoadCancelled()
                return reader, info, archive
            except Exception as error:
                last_error = error
                try:
                    reader.close()
                except Exception:
                    pass
                if session.reader is reader:
                    session.reader = None
                if not queue:
                    break

        if last_error is not None:
            raise last_error
        raise TrawlNetError("Trawl could not open that link.")

    def _scrape(
        self, candidate: str, headers: dict[str, str], session: Session
    ) -> str | None:
        if session.cancel.is_set():
            raise LoadCancelled()
        self._loading_message(session, "Looking for the download link")
        try:
            text, final_url = fetch_page(
                candidate, headers, insecure=session.insecure
            )
        except Exception as error:
            log.debug("Could not fetch landing page %s: %s", candidate, error)
            return None
        return scrape_archive_url(text, final_url)

    def _loading_message(self, session: Session, message: str) -> None:
        GLib.idle_add(self._apply_loading_message, session, message)

    def _apply_loading_message(self, session: Session, message: str) -> bool:
        if session in self.sessions and session.loading:
            if session.status_label is not None:
                session.status_label.set_text(message)
            if self._active_session() is session:
                self._window_title.set_subtitle(_truncate(message))
        return False

    def _load_succeeded(
        self,
        session: Session,
        reader: HTTPRangeReader,
        info: RemoteInfo,
        archive: Archive,
    ) -> bool:
        if session.cancel.is_set() or session not in self.sessions:
            archive.close()
            return False

        session.reader = reader
        session.info = info
        session.archive = archive
        session.loading = False
        session.filename = info.filename or session.filename
        self.launch_log.record_archive(archive.kind, len(archive.files), info.size)

        page = ArchivePage(self, session)
        session.page = page
        session.content_page = page
        self._set_content_page(page, f"archive-{id(session)}")

        try:
            self.store.remember(
                HistoryItem(
                    url=session.url,
                    filename=session.filename,
                    size=info.size,
                    kind=archive.kind,
                    opened_at=time.time(),
                    destination=str(page.destination),
                )
            )
            self._refresh_recent()
        except OSError as error:
            log.debug("Could not save history: %s", error)

        for warning in archive.warnings[:1]:
            self.toast(warning)
        if not archive.warnings:
            self.toast(f"Indexed {human_count(len(archive.files), 'file')}")
        return False

    def _load_failed(self, session: Session, error: Exception) -> bool:
        if session not in self.sessions or session.cancel.is_set():
            return False
        message = friendly_message(error)
        session.loading = False
        session.error = message
        self.launch_log.record_error(error_code(error), type(error).__name__)
        if session.spinner is not None:
            session.spinner.stop()
        page = self._build_error_page(session, message)
        session.content_page = page
        self._set_content_page(page, f"error-{id(session)}")
        return False

    def _build_error_page(self, session: Session, message: str) -> Adw.StatusPage:
        status = Adw.StatusPage(
            icon_name="dialog-error-symbolic",
            title="Could not open this archive",
            description=message,
        )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_halign(Gtk.Align.CENTER)
        retry = Gtk.Button(label="Try again")
        retry.add_css_class("nb-button")
        retry.connect("clicked", lambda *_: self._retry_session(session))
        box.append(retry)
        back = Gtk.Button(label="Back to archives")
        back.add_css_class("nb-secondary")
        back.connect("clicked", lambda *_: self._show_empty())
        box.append(back)
        status.set_child(box)
        return status

    def _discard_session(self, session: Session) -> bool:
        if session in self.sessions:
            self._remove_session(session)
        return False

    def _friendly_error(self, error: Exception) -> str:
        return friendly_message(error)

    # ----------------------------------------------------------------- close
    def close_session(self, session: Session) -> None:
        if session not in self.sessions:
            return
        if session.page is not None and session.page.is_downloading:
            TrawlAlert(
                self,
                "Stop the download?",
                "Files already finished will be kept. "
                "Unfinished files remain as .trawl-part.",
                [
                    ("keep", "Keep downloading", "nb-secondary"),
                    ("stop", "Stop and close", "nb-red"),
                ],
                "keep",
                lambda response, s=session: self._close_confirmed(response, s),
            ).present()
            return
        self._remove_session(session)

    def _close_confirmed(self, response: str, session: Session) -> None:
        if response == "stop":
            self._remove_session(session)

    def _remove_session(self, session: Session) -> None:
        session.cancel.set()
        was_current = self._active_session() is session
        if session.reader is not None:
            try:
                session.reader.close()
            except Exception:
                pass
        if session.page is not None:
            session.page.shutdown()
        if session.content_page is not None and session.content_page.get_parent() is self._stack:
            self._stack.remove(session.content_page)
        try:
            self.sessions.remove(session)
        except ValueError:
            pass
        if was_current:
            if self.sessions:
                self.show_session(self.sessions[-1])
            else:
                self._show_empty()
        else:
            self._update_header()
        self._refresh_session_dropdown()

    # -------------------------------------------------------------- utilities
    def toast(self, message: str) -> None:
        toast = Adw.Toast(title=message, timeout=3)
        self._toast_overlay.add_toast(toast)

    def open_preferences(self) -> None:
        dialog = SettingsDialog(self, self.store)
        dialog.connect("closed", lambda *_: self._refresh_recent())
        dialog.present(self)

    def _on_close_request(self, *_args) -> bool:
        try:
            width = self.get_width() or self.settings.window_width
            height = self.get_height() or self.settings.window_height
            if not self.is_maximized():
                self.settings.window_width = max(640, width)
                self.settings.window_height = max(480, height)
            self.settings.maximized = self.is_maximized()
            self.store.save()
        except Exception:
            pass
        for session in list(self.sessions):
            session.cancel.set()
            if session.page is not None:
                session.page.shutdown()
        self.launch_log.finish()
        return False
