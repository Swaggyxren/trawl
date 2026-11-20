from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, APP_NAME, __version__
from .launchlog import LaunchLog
from .settings import SettingsStore
from .ui.window import MainWindow

log = logging.getLogger(__name__)


def _load_css() -> None:
    display = Gdk.Display.get_default()
    if display is None:
        return
    css_path = Path(__file__).with_name("style.css")
    if not css_path.is_file():
        log.warning("Stylesheet not found: %s", css_path)
        return
    provider = Gtk.CssProvider()
    provider.load_from_path(str(css_path))
    Gtk.StyleContext.add_provider_for_display(
        display,
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )


def _register_source_icons() -> None:
    display = Gdk.Display.get_default()
    if display is None:
        return
    for base in (
        Path(__file__).resolve().parents[2] / "data" / "icons",
        Path(sys.prefix) / "share" / "icons",
    ):
        if base.is_dir():
            Gtk.IconTheme.get_for_display(display).add_search_path(str(base))


class TrawlApplication(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self.store = SettingsStore()
        self.launch_log = LaunchLog()
        self.window: MainWindow | None = None
        self._startup_done = False

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        # Neubrutalism uses a paper background and black ink, so the light
        # palette is part of the design rather than a user preference toggle.
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        _register_source_icons()
        _load_css()
        self._install_actions()
        self.launch_log.start()
        self._startup_done = True

    def do_shutdown(self) -> None:
        # Finalize the diagnostic log even if the window was never closed
        # explicitly (for example after app.quit()).
        self.launch_log.finish()
        Adw.Application.do_shutdown(self)

    def _install_actions(self) -> None:
        actions = {
            "add-link": self._action_add_link,
            "preferences": self._action_preferences,
            "about": self._action_about,
            "quit": lambda *_: self.quit(),
        }
        for name, callback in actions.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)

        open_folder = Gio.SimpleAction.new(
            "open-folder", GLib.VariantType.new("s")
        )
        open_folder.connect("activate", self._action_open_folder)
        self.add_action(open_folder)

        self.set_accels_for_action("app.add-link", ["<primary>l", "<primary>n"])
        self.set_accels_for_action("app.preferences", ["<primary>comma"])
        self.set_accels_for_action("app.quit", ["<primary>q"])

    def _action_add_link(self, *_args) -> None:
        self.activate()
        if self.window is not None:
            self.window.add_link()

    def _action_preferences(self, *_args) -> None:
        self.activate()
        if self.window is not None:
            self.window.open_preferences()

    def _action_open_folder(self, _action, parameter) -> None:
        self.activate()
        path = Path(parameter.get_string())
        try:
            uri = Gio.File.new_for_path(str(path)).get_uri()
            Gtk.UriLauncher.new(uri=uri).launch(self.window, None, None)
        except Exception:
            log.debug("Could not open download folder: %s", path)

    def _action_about(self, *_args) -> None:
        dialog = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon=APP_ID,
            developer_name="Swaggyxren",
            developers=["Swaggyxren"],
            version=__version__,
            comments=(
                "Pull files out of remote archives without downloading the archive.\n\n"
                "Trawl reads a ZIP, RAR or ISO index over HTTP byte ranges and fetches "
                "only the files you select. Inspired by ZipStream for Android (MIT)."
            ),
            website="https://github.com/Swaggyxren/trawl",
            issue_url="https://github.com/Swaggyxren/trawl/issues",
            license_type=Gtk.License.MIT_X11,
            copyright="© 2026 Swaggyxren",
        )
        dialog.present(self.window)

    def do_activate(self) -> None:
        if self.window is None:
            self.window = MainWindow(self, self.store)
        self.window.present()

    def do_command_line(self, command_line) -> int:
        args = command_line.get_arguments()
        url = None
        for argument in args[1:]:
            if argument in ("--version", "-v"):
                print(f"Trawl {__version__}")
                return 0
            if argument.startswith("-"):
                continue
            url = argument
            break
        self.activate()
        if url and self.window is not None:
            self.window.open_url(url)
        return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("TRAWL_LOG_LEVEL", "WARNING").upper(),
        format="%(levelname)s %(name)s: %(message)s",
    )
    app = TrawlApplication()
    return app.run(argv if argv is not None else sys.argv)
