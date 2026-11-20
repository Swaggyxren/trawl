"""GTK 4 / libadwaita user interface for Trawl."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # noqa: E402


def name_icon(widget: Gtk.Widget, label: str) -> None:
    """Give an icon-only control a tooltip and an accessible name."""
    widget.set_tooltip_text(label)
    try:
        widget.update_property(Gtk.AccessibleProperty.LABEL, label)
    except Exception:
        pass
