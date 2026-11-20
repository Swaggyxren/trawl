from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from gi.repository import Gdk, Gio, GLib

log = logging.getLogger(__name__)

_SOUND_FILES = {
    "complete": "/usr/share/sounds/freedesktop/stereo/complete.oga",
    "dialog-error": "/usr/share/sounds/freedesktop/stereo/dialog-error.oga",
}


def play_sound(event: str, enabled: bool) -> None:
    """Play a freedesktop sound without blocking the UI; ignore all errors."""
    if not enabled:
        return

    def run() -> None:
        try:
            import gi

            gi.require_version("GSound", "1.0")
            from gi.repository import GSound

            context = GSound.Context.new()
            context.play_simple({"event.id": event})
            return
        except Exception:
            pass

        try:
            subprocess.Popen(
                ["canberra-gtk-play", "-i", event],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except Exception:
            pass

        sound = _SOUND_FILES.get(event)
        if sound and Path(sound).is_file():
            try:
                subprocess.Popen(
                    ["paplay", sound],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
            except Exception:
                pass

        try:
            display = Gdk.Display.get_default()
            if display is not None:
                display.beep()
        except Exception:
            pass

    # Run off the main loop; GLib.idle_add is safe from worker threads.
    GLib.idle_add(lambda: (run(), False)[1])


def send_notification(
    app: Gio.Application, body: str, destination: str, enabled: bool
) -> None:
    """Send a desktop notification with an Open folder action."""
    if not enabled:
        return
    try:
        notification = Gio.Notification.new("Trawl — Download finished")
        notification.set_body(body)
        notification.set_default_action_and_target(
            "app.open-folder", GLib.Variant("s", str(destination))
        )
        app.send_notification("download-finished", notification)
        return
    except Exception as error:
        log.debug("Gio notification failed, falling back to notify-send: %s", error)

    try:
        subprocess.Popen(
            ["notify-send", "Trawl — Download finished", body],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as error:
        log.debug("notify-send failed: %s", error)
