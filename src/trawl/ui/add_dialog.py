from __future__ import annotations

from typing import Callable

from gi.repository import Adw, Gtk

from ..net import INVALID_LINK_MESSAGE, validate_link
from ..util import parse_header_lines
from . import name_icon

SUPPORTED_TEXT = (
    "ZIP and ZIP64, stored RAR, and ISO 9660 images (including Joliet and Rock Ridge). "
    "Links from Pixeldrain, MediaFire, Buzzheavier, Dropbox and direct downloads work best."
)


class AddLinkDialog(Adw.Dialog):
    """Collects a link and per-download network options."""

    def __init__(
        self,
        parent,
        settings,
        on_submit: Callable[[str, str, bool, bool], None],
        initial_url: str = "",
    ):
        super().__init__()
        self.add_css_class("nb-root")
        self._settings = settings
        self._on_submit = on_submit
        self.set_title("Open a remote archive")
        self.set_content_width(560)
        self.set_content_height(460)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        title = Adw.WindowTitle(title="Open a remote archive")
        header.set_title_widget(title)
        toolbar.add_top_bar(header)

        clamp = Adw.Clamp(maximum_size=540)
        clamp.set_margin_top(12)
        clamp.set_margin_bottom(24)
        clamp.set_margin_start(12)
        clamp.set_margin_end(12)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        clamp.set_child(box)

        intro = Gtk.Label(
            label="Paste a link to a ZIP, RAR or ISO file. Trawl reads only the "
            "archive index, then fetches the files you pick.",
            wrap=True,
            xalign=0,
        )
        intro.add_css_class("dim-label")
        box.append(intro)

        entry_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._url_entry = Gtk.Entry()
        self._url_entry.add_css_class("nb-input")
        self._url_entry.set_placeholder_text("https://example.com/files/archive.zip")
        self._url_entry.set_hexpand(True)
        self._url_entry.set_input_purpose(Gtk.InputPurpose.URL)
        self._url_entry.connect("changed", self._on_changed)
        self._url_entry.connect("activate", lambda *_: self._submit())
        entry_box.append(self._url_entry)

        paste_button = Gtk.Button(icon_name="edit-paste-symbolic")
        paste_button.add_css_class("nb-icon-button")
        name_icon(paste_button, "Paste from clipboard")
        paste_button.connect("clicked", self._paste)
        entry_box.append(paste_button)
        box.append(entry_box)

        self._error = Gtk.Label(xalign=0, wrap=True)
        self._error.add_css_class("nb-error")
        self._error.set_visible(False)
        box.append(self._error)

        advanced = Adw.ExpanderRow(title="Advanced")
        advanced.add_css_class("nb-panel")
        advanced.set_subtitle("Custom headers and connection options")

        headers_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        headers_box.set_margin_top(6)
        headers_box.set_margin_bottom(6)
        headers_box.set_margin_start(6)
        headers_box.set_margin_end(6)
        headers_label = Gtk.Label(
            label="Custom HTTP headers, one per line (for example a Referer or Cookie).",
            xalign=0,
            wrap=True,
        )
        headers_label.add_css_class("caption")
        headers_label.add_css_class("dim-label")
        headers_box.append(headers_label)
        frame = Gtk.Frame()
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_size_request(-1, 88)
        scroller.set_has_frame(True)
        self._headers_view = Gtk.TextView()
        self._headers_view.add_css_class("nb-input")
        self._headers_view.set_monospace(True)
        self._headers_view.set_top_margin(6)
        self._headers_view.set_bottom_margin(6)
        self._headers_view.set_left_margin(8)
        self._headers_view.set_right_margin(8)
        self._headers_view.get_buffer().set_text(settings.headers)
        scroller.set_child(self._headers_view)
        frame.set_child(scroller)
        headers_box.append(frame)
        advanced.add_row(headers_box)

        self._insecure = Adw.SwitchRow(
            title="Allow invalid certificates",
            subtitle="Only use this for a host you trust",
            active=settings.insecure,
        )
        self._insecure.add_css_class("nb-panel")
        advanced.add_row(self._insecure)
        self._slow = Adw.SwitchRow(
            title="Slow mode",
            subtitle="Pause briefly between requests to avoid rate limits",
            active=settings.slow_mode,
        )
        self._slow.add_css_class("nb-panel")
        advanced.add_row(self._slow)
        advanced_group = Adw.PreferencesGroup()
        advanced_group.add_css_class("nb-panel")
        advanced_group.add(advanced)
        box.append(advanced_group)

        note = Gtk.Label(label=SUPPORTED_TEXT, xalign=0, wrap=True)
        note.add_css_class("caption")
        note.add_css_class("dim-label")
        box.append(note)

        toolbar.set_content(clamp)

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bottom.set_halign(Gtk.Align.END)
        bottom.set_margin_top(8)
        bottom.set_margin_bottom(12)
        bottom.set_margin_start(12)
        bottom.set_margin_end(12)
        cancel = Gtk.Button(label="Cancel")
        cancel.add_css_class("nb-secondary")
        cancel.connect("clicked", lambda *_: self.close())
        bottom.append(cancel)
        self._open_button = Gtk.Button(label="Open archive")
        self._open_button.add_css_class("nb-button")
        self._open_button.add_css_class("nb-pink")
        self._open_button.set_sensitive(False)
        self._open_button.connect("clicked", lambda *_: self._submit())
        bottom.append(self._open_button)
        toolbar.add_bottom_bar(bottom)

        self.set_child(toolbar)
        if initial_url:
            self._url_entry.set_text(initial_url)
        self._url_entry.grab_focus()

    def _on_changed(self, *_args) -> None:
        text = self._url_entry.get_text()
        if not text.strip():
            self._open_button.set_sensitive(False)
            self._error.set_visible(False)
            return
        url, _message = validate_link(text)
        self._open_button.set_sensitive(bool(url))
        self._error.set_label(INVALID_LINK_MESSAGE)
        self._error.set_visible(not url)

    def _paste(self, *_args) -> None:
        clipboard = self.get_clipboard()
        clipboard.read_text_async(None, self._on_paste_finished)

    def _on_paste_finished(self, clipboard, result) -> None:
        try:
            text = clipboard.read_text_finish(result)
        except Exception:
            return
        if text:
            self._url_entry.set_text(text.strip().splitlines()[0].strip())
            self._url_entry.set_position(-1)

    def _submit(self) -> None:
        text = self._url_entry.get_text().strip()
        if not text:
            return
        url, message = validate_link(text)
        if not url:
            self._show_error(message)
            return
        buffer = self._headers_view.get_buffer()
        headers_text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        try:
            parse_header_lines(headers_text)
        except ValueError as error:
            self._show_error(str(error))
            return
        headers = headers_text
        insecure = self._insecure.get_active()
        slow = self._slow.get_active()
        self.close()
        self._on_submit(url, headers, insecure, slow)

    def _show_error(self, message: str) -> None:
        self._error.set_text(message)
        self._error.set_visible(True)
