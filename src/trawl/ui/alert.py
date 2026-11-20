from __future__ import annotations

from typing import Callable

from gi.repository import Adw, Gtk


class TrawlAlert(Adw.Window):
    """Square, fully CSS-controlled replacement for Adw.AlertDialog.

    libadwaita paints the AlertDialog sheet and its internal gizmos white on a
    dark surface, and those nodes cannot be reliably restyled from CSS. Using
    a modal Adw.Window keeps the whole popup under our ``nb-root`` stylesheet.
    """

    def __init__(
        self,
        parent: Gtk.Window | None,
        heading: str,
        body: str,
        responses: list[tuple[str, str, str]],
        default: str,
        on_response: Callable[[str], None],
    ):
        super().__init__(transient_for=parent, modal=True)
        self.add_css_class("nb-root")
        self.add_css_class("nb-alert")
        self.set_title(heading)
        self.set_default_size(480, -1)
        self.set_resizable(False)
        self._default = default
        self._on_response = on_response
        self._responded = False

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        header.set_title_widget(Adw.WindowTitle(title=heading))
        toolbar.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_margin_top(20)
        box.set_margin_bottom(22)
        box.set_margin_start(24)
        box.set_margin_end(24)

        label = Gtk.Label(label=body, wrap=True, xalign=0)
        label.add_css_class("nb-alert-body")
        box.append(label)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        buttons.set_halign(Gtk.Align.END)
        for response_id, text, style in responses:
            button = Gtk.Button(label=text)
            for css_class in style.split():
                button.add_css_class(css_class)
            button.connect("clicked", lambda *_value, rid=response_id: self._respond(rid))
            buttons.append(button)
        box.append(buttons)
        toolbar.set_content(box)
        self.set_content(toolbar)
        self.connect("close-request", lambda *_: self._respond(self._default))

    def _respond(self, response: str) -> None:
        if self._responded:
            return
        self._responded = True
        try:
            self._on_response(response)
        finally:
            self.destroy()
