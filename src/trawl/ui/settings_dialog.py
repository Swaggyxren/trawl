from __future__ import annotations

from pathlib import Path

from gi.repository import Adw, Gio, Gtk

from ..settings import SettingsStore
from ..util import tilde_path
from .alert import TrawlAlert


class SettingsDialog(Adw.PreferencesDialog):
    def __init__(self, parent, store: SettingsStore):
        super().__init__()
        self.add_css_class("nb-root")
        self._store = store
        self._settings = store.settings
        self._parent = parent
        self.set_title("Preferences")

        page = Adw.PreferencesPage()
        page.set_title("General")
        page.set_icon_name("emblem-system-symbolic")

        downloads = Adw.PreferencesGroup(title="Downloads")
        downloads.set_description("Where and how files are written")
        page.add(downloads)
        downloads.add_css_class("nb-panel")

        self._folder_row = Adw.ActionRow(
            title="Save to",
            subtitle=tilde_path(self._settings.effective_download_dir()),
        )
        self._folder_row.set_activatable(True)
        choose = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        choose.add_css_class("nb-secondary")
        choose.connect("clicked", lambda *_: self._choose_folder())
        self._folder_row.add_suffix(choose)
        self._folder_row.connect("activated", lambda *_: self._choose_folder())
        downloads.add(self._folder_row)

        adjustment = Gtk.Adjustment(
            value=self._settings.threads, lower=1, upper=16, step_increment=1, page_increment=2
        )
        threads = Adw.SpinRow(
            title="Parallel downloads",
            subtitle="Connections used at the same time",
            adjustment=adjustment,
        )
        threads.connect("notify::value", self._threads_changed)
        downloads.add(threads)

        self._skip = Adw.SwitchRow(
            title="Skip files that already exist",
            active=self._settings.skip_existing,
        )
        self._skip.connect("notify::active", self._skip_changed)
        downloads.add(self._skip)

        self._overwrite = Adw.SwitchRow(
            title="Overwrite existing files",
            subtitle="Replaces files with the same name in the destination",
            active=self._settings.overwrite,
        )
        self._overwrite.connect("notify::active", self._overwrite_changed)
        downloads.add(self._overwrite)

        self._resume = Adw.SwitchRow(
            title="Resume interrupted downloads",
            subtitle="Uses .trawl-part files for stored entries",
            active=self._settings.resume,
        )
        self._resume.connect("notify::active", self._save_flag("resume"))
        downloads.add(self._resume)

        self._verify = Adw.SwitchRow(
            title="Verify checksums",
            subtitle="Checks CRC-32 after each file when the archive provides one",
            active=self._settings.verify,
        )
        self._verify.connect("notify::active", self._save_flag("verify"))
        downloads.add(self._verify)

        self._mtime = Adw.SwitchRow(
            title="Preserve modification times",
            active=self._settings.preserve_mtime,
        )
        self._mtime.connect("notify::active", self._save_flag("preserve_mtime"))
        downloads.add(self._mtime)

        self._choice_skip = Adw.SwitchRow(
            title="Skip the choice screen and open the file list",
            subtitle="Go straight to the file browser when an archive opens",
            active=self._settings.choice_skip,
        )
        self._choice_skip.add_css_class("nb-panel")
        self._choice_skip.connect("notify::active", self._save_flag("choice_skip"))
        downloads.add(self._choice_skip)

        self._notify_finish = Adw.SwitchRow(
            title="Notify when a download finishes",
            active=self._settings.notify_finish,
        )
        self._notify_finish.add_css_class("nb-panel")
        self._notify_finish.connect("notify::active", self._save_flag("notify_finish"))
        downloads.add(self._notify_finish)

        self._play_sound = Adw.SwitchRow(
            title="Play a sound",
            subtitle="Freedesktop completion or error sound",
            active=self._settings.play_sound,
        )
        self._play_sound.add_css_class("nb-panel")
        self._play_sound.connect("notify::active", self._save_flag("play_sound"))
        downloads.add(self._play_sound)

        network = Adw.PreferencesGroup(title="Network")
        network.set_description("Options for difficult hosts")
        page.add(network)
        network.add_css_class("nb-panel")

        self._slow = Adw.SwitchRow(
            title="Slow mode",
            subtitle="Space out requests when a server rate-limits you",
            active=self._settings.slow_mode,
        )
        self._slow.connect("notify::active", self._save_flag("slow_mode"))
        network.add(self._slow)

        self._insecure = Adw.SwitchRow(
            title="Allow invalid certificates",
            subtitle="Do not use this unless you trust the host",
            active=self._settings.insecure,
        )
        self._insecure.connect("notify::active", self._save_flag("insecure"))
        network.add(self._insecure)

        headers = Adw.ExpanderRow(title="Custom HTTP headers")
        headers.set_subtitle("One Name: value per line")
        headers_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        headers_box.set_margin_top(6)
        headers_box.set_margin_bottom(6)
        headers_box.set_margin_start(6)
        headers_box.set_margin_end(6)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_size_request(-1, 110)
        scroller.set_has_frame(True)
        self._headers_view = Gtk.TextView()
        self._headers_view.add_css_class("nb-input")
        self._headers_view.set_monospace(True)
        self._headers_view.set_top_margin(6)
        self._headers_view.set_bottom_margin(6)
        self._headers_view.set_left_margin(8)
        self._headers_view.set_right_margin(8)
        self._headers_view.get_buffer().set_text(self._settings.headers)
        self._headers_view.get_buffer().connect("changed", self._headers_changed)
        scroller.set_child(self._headers_view)
        headers_box.append(scroller)
        headers.add_row(headers_box)
        network.add(headers)

        privacy = Adw.PreferencesGroup(title="Privacy")
        page.add(privacy)
        privacy.add_css_class("nb-panel")
        history_row = Adw.ActionRow(
            title="Recent archives",
            subtitle=f"{len(self._settings.history)} link(s) remembered",
        )
        clear = Gtk.Button(label="Clear", valign=Gtk.Align.CENTER)
        clear.add_css_class("nb-secondary")
        clear.add_css_class("nb-red")
        clear.connect("clicked", lambda *_: self._clear_history())
        history_row.add_suffix(clear)
        privacy.add(history_row)

        about = Adw.PreferencesGroup(title="About")
        page.add(about)
        about.add_css_class("nb-panel")
        about_row = Adw.ActionRow(
            title="Trawl",
            subtitle="Pull files out of remote archives without downloading the archive.",
        )
        from .. import __version__

        version = Gtk.Label(label=__version__)
        version.add_css_class("nb-tag")
        about_row.add_suffix(version)
        about.add(about_row)
        inspiration = Adw.ActionRow(
            title="Inspired by ZipStream",
            subtitle="An Android experiment in remote archive browsing (MIT)",
        )
        inspiration.set_activatable(True)
        inspiration.connect(
            "activated",
            lambda *_: Gtk.UriLauncher.new(
                uri="https://github.com/Yexclsv25/ZipStream"
            ).launch(self._parent, None, None),
        )
        about.add(inspiration)

        self.add(page)
        self.connect("closed", lambda *_: self._store.save())

    # ------------------------------------------------------------------ events
    def _save_flag(self, name: str):
        def handler(row, _pspec):
            setattr(self._settings, name, row.get_active())

        return handler

    def _threads_changed(self, row, _pspec) -> None:
        try:
            self._settings.threads = max(1, min(16, int(row.get_value())))
        except (TypeError, ValueError):
            pass

    def _skip_changed(self, row, _pspec) -> None:
        enabled = row.get_active()
        self._settings.skip_existing = enabled
        if enabled and self._overwrite.get_active():
            self._overwrite.set_active(False)

    def _overwrite_changed(self, row, _pspec) -> None:
        enabled = row.get_active()
        self._settings.overwrite = enabled
        if enabled and self._skip.get_active():
            self._skip.set_active(False)

    def _headers_changed(self, buffer) -> None:
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        self._settings.headers = text

    def _choose_folder(self) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose a download folder")
        current = Path(self._settings.effective_download_dir())
        try:
            dialog.set_initial_folder(Gio.File.new_for_path(str(current)))
        except Exception:
            pass
        dialog.select_folder(self._parent, None, self._folder_chosen)

    def _folder_chosen(self, dialog, result) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except Exception:
            return
        if folder is None:
            return
        path = folder.get_path()
        if not path:
            return
        self._settings.download_dir = path
        self._folder_row.set_subtitle(tilde_path(path))

    def _clear_history(self) -> None:
        TrawlAlert(
            self._parent,
            "Clear recent archives?",
            "This only removes the links from Trawl. Downloaded files stay on disk.",
            [
                ("cancel", "Cancel", "nb-secondary"),
                ("clear", "Clear", "nb-red"),
            ],
            "cancel",
            self._history_cleared,
        ).present()

    def _history_cleared(self, response: str) -> None:
        if response == "clear":
            self._store.clear_history()
            self._settings.history.clear()
