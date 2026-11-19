#!/usr/bin/env python3
"""
M3U TUI Editor
A clean, modern terminal user interface for viewing and editing M3U playlists.

Key Features:
- Load & save M3U files (UTF-8 encoding)
- Clean, responsive table view with instant column sorting by clicking headers
- Filter by "group-title" and live full-text search
- ENTER: Edit selected channel in a modal form
- SPACE: Select / unselect channels for batch operations
- G / Ctrl+G: Batch modify "group-title" for all selected channels
- Ctrl+A: Select all visible channels, Ctrl+D: Clear selections
- DEL: Delete channel with clean confirmation
- INS: Add a new channel
- Ctrl+S: Save file, Ctrl+O: Open another file
"""

import os
import sys
import re
import time
import json
import concurrent.futures
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional, Set

import requests
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, Grid
from textual.screen import ModalScreen
from textual.widgets import (
    Header,
    Footer,
    DataTable,
    Input,
    Button,
    Label,
    Select,
    Static,
)
from textual import on, work


# -----------------------------------------------------------------------------
# Data Model & M3U Parser
# -----------------------------------------------------------------------------

@dataclass
class M3UItem:
    title: str
    url: str
    duration: str = "-1"
    group_title: str = ""
    tvg_id: str = ""
    tvg_logo: str = ""
    extra_attrs: Dict[str, str] = field(default_factory=dict)
    raw_extra_lines: List[str] = field(default_factory=list)

    def to_extinf(self) -> str:
        attrs = []
        if self.tvg_id is not None:
            attrs.append(f'tvg-id="{self.tvg_id}"')
        if self.tvg_logo is not None and self.tvg_logo != "":
            attrs.append(f'tvg-logo="{self.tvg_logo}"')
        for k, v in self.extra_attrs.items():
            if k not in ("tvg-id", "tvg-logo", "group-title"):
                attrs.append(f'{k}="{v}"')
        if self.group_title:
            attrs.append(f'group-title="{self.group_title}"')

        attr_str = (" " + " ".join(attrs)) if attrs else ""
        return f"#EXTINF:{self.duration or '-1'}{attr_str},{self.title}"


def parse_extinf_line(line: str) -> Tuple[str, Dict[str, str], str]:
    """Parse #EXTINF line: duration, key-value attributes, and channel name."""
    if line.startswith("#EXTINF:"):
        rest = line[8:].strip()
    elif line.startswith("#EXTINF"):
        rest = line[7:].lstrip(":").strip()
    else:
        return "-1", {}, ""

    m = re.match(r'^(-?[\d\.]+)\s*(.*)', rest)
    if m:
        duration = m.group(1)
        rest = m.group(2)
    else:
        duration = "-1"

    in_quotes = False
    quote_char = None
    comma_idx = -1
    for i, ch in enumerate(rest):
        if ch in ('"', "'"):
            if not in_quotes:
                in_quotes = True
                quote_char = ch
            elif quote_char == ch:
                in_quotes = False
                quote_char = None
        elif ch == ',' and not in_quotes:
            comma_idx = i
            break

    if comma_idx != -1:
        attrs_part = rest[:comma_idx].strip()
        title = rest[comma_idx + 1:].strip()
    else:
        attrs_part = rest
        title = ""

    attrs = {}
    attr_pattern = re.compile(r'([a-zA-Z0-9_\-:]+)=(?:"([^"]*)"|\'([^\']*)\'|([^\s,]+))')
    for match in attr_pattern.finditer(attrs_part):
        key = match.group(1)
        val = match.group(2) if match.group(2) is not None else (match.group(3) if match.group(3) is not None else match.group(4))
        attrs[key] = val or ""

    return duration, attrs, title


def load_m3u(file_path: str) -> Tuple[str, List[M3UItem]]:
    """Load and parse an M3U file."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        lines = [line.strip() for line in f if line.strip()]

    header = "#EXTM3U"
    items: List[M3UItem] = []
    i = 0
    if lines and lines[0].startswith("#EXTM3U"):
        header = lines[0]
        i = 1

    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXTINF"):
            duration, attrs, title = parse_extinf_line(line)
            group_title = attrs.pop("group-title", "")
            tvg_id = attrs.pop("tvg-id", "")
            tvg_logo = attrs.pop("tvg-logo", "")
            i += 1

            extra_lines = []
            while i < len(lines) and lines[i].startswith("#") and not lines[i].startswith("#EXTINF"):
                extra_lines.append(lines[i])
                i += 1

            url = lines[i] if i < len(lines) else ""
            i += 1

            items.append(M3UItem(
                title=title,
                url=url,
                duration=duration,
                group_title=group_title,
                tvg_id=tvg_id,
                tvg_logo=tvg_logo,
                extra_attrs=attrs,
                raw_extra_lines=extra_lines
            ))
        else:
            i += 1

    return header, items


def save_m3u(file_path: str, header: str, items: List[M3UItem]) -> None:
    """Save playlist to standard M3U format with UTF-8 encoding."""
    with open(file_path, "w", encoding="utf-8", newline="\n") as f:
        f.write((header or "#EXTM3U") + "\n")
        for item in items:
            f.write(item.to_extinf() + "\n")
            for extra in item.raw_extra_lines:
                f.write(extra + "\n")
            f.write(item.url + "\n")


def check_single_stream(url: str, timeout: float = 3.5) -> Tuple[str, float, str]:
    """
    Checks if a stream URL is reachable and measures latency.
    Returns: (status_type, elapsed_ms, label)
    status_type: 'FAST' (< 1500ms) | 'SLOW' (>= 1500ms) | 'OFFLINE'
    """
    if not url or not url.startswith(("http://", "https://")):
        return "OFFLINE", 0.0, "Invalid URL"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "*/*",
    }

    start = time.perf_counter()
    try:
        resp = requests.get(
            url,
            headers=headers,
            timeout=timeout,
            stream=True,
            allow_redirects=True
        )
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        status_code = resp.status_code
        resp.close()

        if 200 <= status_code < 400:
            if elapsed_ms < 1500:
                return "FAST", elapsed_ms, f"OK {int(elapsed_ms)}ms"
            else:
                return "SLOW", elapsed_ms, f"Slow {elapsed_ms / 1000.0:.1f}s"
        else:
            return "OFFLINE", elapsed_ms, f"HTTP {status_code}"
    except requests.exceptions.Timeout:
        return "OFFLINE", timeout * 1000.0, "Timeout"
    except requests.exceptions.ConnectionError:
        return "OFFLINE", 0.0, "Offline"
    except Exception:
        return "OFFLINE", 0.0, "Error"


# -----------------------------------------------------------------------------
# Modals
# -----------------------------------------------------------------------------

class ItemEditorModal(ModalScreen[Optional[M3UItem]]):
    """Channel edit or create modal dialog."""

    DEFAULT_CSS = """
    ItemEditorModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.65);
    }

    #editor-box {
        width: 80;
        max-width: 95%;
        height: auto;
        border: heavy $accent;
        background: $surface;
        padding: 1 2;
    }

    #editor-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
        text-align: center;
        border-bottom: solid $accent-muted;
    }

    .form-row {
        height: auto;
        margin-bottom: 1;
    }

    .field-label {
        width: 16;
        padding-top: 1;
        color: $text-muted;
        text-style: bold;
    }

    .field-input {
        width: 1fr;
    }

    #button-row {
        margin-top: 1;
        align: right middle;
        height: auto;
    }

    #button-row Button {
        margin-left: 2;
        min-width: 12;
    }

    #error-label {
        color: $error;
        text-style: bold;
        margin-top: 1;
        height: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
    ]

    def __init__(self, item: Optional[M3UItem] = None, initial_group: str = "", is_new: bool = False):
        super().__init__()
        self.item = item
        self.initial_group = initial_group
        self.is_new = is_new

    def compose(self) -> ComposeResult:
        with Vertical(id="editor-box"):
            title_text = "New Channel" if self.is_new else f"Edit Channel: {self.item.title if self.item else ''}"
            yield Label(title_text, id="editor-title")

            with Horizontal(classes="form-row"):
                yield Label("Name *:", classes="field-label")
                yield Input(
                    value=self.item.title if self.item else "",
                    placeholder="e.g. HBO HD, BBC One...",
                    id="input-title",
                    classes="field-input"
                )

            with Horizontal(classes="form-row"):
                yield Label("Group:", classes="field-label")
                default_group = (self.item.group_title if self.item else self.initial_group) or ""
                yield Input(
                    value=default_group,
                    placeholder="e.g. Movies, Sports, News...",
                    id="input-group",
                    classes="field-input"
                )

            with Horizontal(classes="form-row"):
                yield Label("Stream URL *:", classes="field-label")
                yield Input(
                    value=self.item.url if self.item else "",
                    placeholder="http(s)://.../playlist.m3u8",
                    id="input-url",
                    classes="field-input"
                )

            with Horizontal(classes="form-row"):
                yield Label("EPG ID:", classes="field-label")
                yield Input(
                    value=self.item.tvg_id if self.item else "",
                    placeholder="e.g. MediaPressTV_119",
                    id="input-tvg-id",
                    classes="field-input"
                )

            with Horizontal(classes="form-row"):
                yield Label("Logo URL:", classes="field-label")
                yield Input(
                    value=self.item.tvg_logo if self.item else "",
                    placeholder="https://.../logo.png",
                    id="input-tvg-logo",
                    classes="field-input"
                )

            with Horizontal(classes="form-row"):
                yield Label("Duration:", classes="field-label")
                yield Input(
                    value=self.item.duration if (self.item and self.item.duration) else "-1",
                    placeholder="-1",
                    id="input-duration",
                    classes="field-input"
                )

            yield Label("", id="error-label")

            with Horizontal(id="button-row"):
                action_text = "Add" if self.is_new else "Save"
                yield Button(action_text, variant="primary", id="btn-save")
                yield Button("Cancel", variant="default", id="btn-cancel")

    def on_mount(self) -> None:
        self.query_one("#input-title", Input).focus()

    @on(Button.Pressed, "#btn-save")
    def on_save_clicked(self) -> None:
        self.action_save()

    @on(Button.Pressed, "#btn-cancel")
    def on_cancel_clicked(self) -> None:
        self.action_cancel()

    @on(Input.Submitted)
    def on_input_submitted(self) -> None:
        self.action_save()

    def action_save(self) -> None:
        title = self.query_one("#input-title", Input).value.strip()
        url = self.query_one("#input-url", Input).value.strip()
        group = self.query_one("#input-group", Input).value.strip()
        tvg_id = self.query_one("#input-tvg-id", Input).value.strip()
        tvg_logo = self.query_one("#input-tvg-logo", Input).value.strip()
        duration = self.query_one("#input-duration", Input).value.strip() or "-1"

        error_label = self.query_one("#error-label", Label)

        if not title:
            error_label.update("Channel name cannot be empty!")
            self.query_one("#input-title", Input).focus()
            return

        if not url:
            error_label.update("Stream URL cannot be empty!")
            self.query_one("#input-url", Input).focus()
            return

        extra_attrs = dict(self.item.extra_attrs) if (self.item and self.item.extra_attrs) else {}
        raw_extra = list(self.item.raw_extra_lines) if (self.item and self.item.raw_extra_lines) else []

        saved_item = M3UItem(
            title=title,
            url=url,
            duration=duration,
            group_title=group,
            tvg_id=tvg_id,
            tvg_logo=tvg_logo,
            extra_attrs=extra_attrs,
            raw_extra_lines=raw_extra
        )
        self.dismiss(saved_item)

    def action_cancel(self) -> None:
        self.dismiss(None)


class BatchGroupModal(ModalScreen[Optional[str]]):
    """Batch group-title assignment modal dialog."""

    DEFAULT_CSS = """
    BatchGroupModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.65);
    }

    #batch-box {
        width: 72;
        max-width: 95%;
        height: auto;
        border: heavy $accent;
        background: $surface;
        padding: 1 2;
    }

    #batch-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
        text-align: center;
        border-bottom: solid $accent-muted;
    }

    #batch-info {
        margin-bottom: 1;
        color: $text;
        background: $panel;
        padding: 1;
        border: solid $panel-lighten-2;
    }

    .form-row {
        height: auto;
        margin-bottom: 1;
    }

    .field-label {
        width: 18;
        padding-top: 1;
        color: $text-muted;
        text-style: bold;
    }

    .field-input {
        width: 1fr;
    }

    #batch-buttons {
        margin-top: 1;
        align: right middle;
    }

    #batch-buttons Button {
        margin-left: 2;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, selected_count: int, existing_groups: List[str]):
        super().__init__()
        self.selected_count = selected_count
        self.existing_groups = sorted([g for g in existing_groups if g], key=lambda s: s.lower())

    def compose(self) -> ComposeResult:
        with Vertical(id="batch-box"):
            yield Label("Batch Set Group", id="batch-title")
            yield Label(
                f"Selected channels: [bold yellow]{self.selected_count}[/bold yellow]\n"
                f"Pick an existing group or type a new one below:",
                id="batch-info"
            )

            options = [("--- Choose existing group (optional) ---", "")]
            for g in self.existing_groups:
                options.append((g, g))

            with Horizontal(classes="form-row"):
                yield Label("Existing groups:", classes="field-label")
                yield Select(
                    options=options,
                    value="",
                    id="select-existing-group",
                    classes="field-input",
                    allow_blank=False
                )

            with Horizontal(classes="form-row"):
                yield Label("New group name *:", classes="field-label")
                yield Input(
                    placeholder="Type new group name...",
                    id="input-batch-group",
                    classes="field-input"
                )

            with Horizontal(id="batch-buttons"):
                yield Button(f"Apply ({self.selected_count})", variant="primary", id="btn-apply")
                yield Button("Cancel", variant="default", id="btn-cancel")

    def on_mount(self) -> None:
        self.query_one("#input-batch-group", Input).focus()

    @on(Select.Changed, "#select-existing-group")
    def on_select_group(self, event: Select.Changed) -> None:
        if event.value and event.value != "":
            self.query_one("#input-batch-group", Input).value = str(event.value)

    @on(Button.Pressed, "#btn-apply")
    def on_apply(self) -> None:
        self.action_submit()

    @on(Input.Submitted, "#input-batch-group")
    def on_submit_input(self) -> None:
        self.action_submit()

    def action_submit(self) -> None:
        val = self.query_one("#input-batch-group", Input).value.strip()
        self.dismiss(val)

    @on(Button.Pressed, "#btn-cancel")
    def on_cancel(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmDeleteModal(ModalScreen[bool]):
    """Clean delete confirmation dialog."""

    DEFAULT_CSS = """
    ConfirmDeleteModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.7);
    }

    #confirm-box {
        width: 64;
        height: auto;
        border: heavy $error;
        background: $surface;
        padding: 1 2;
    }

    #confirm-title {
        color: $error;
        text-style: bold;
        margin-bottom: 1;
        text-align: center;
    }

    #confirm-item-info {
        background: $panel;
        padding: 1;
        margin-bottom: 1;
        border: solid $panel-lighten-2;
    }

    #confirm-buttons {
        align: right middle;
        height: auto;
    }

    #confirm-buttons Button {
        margin-left: 2;
        min-width: 14;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("n", "cancel", "Cancel"),
        Binding("enter", "confirm", "Delete"),
        Binding("y", "confirm", "Delete"),
        Binding("delete", "confirm", "Delete"),
    ]

    def __init__(self, item: M3UItem):
        super().__init__()
        self.item = item

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label("CONFIRM DELETE", id="confirm-title")
            yield Label("Are you sure you want to delete this channel?")
            with Vertical(id="confirm-item-info"):
                yield Label(f"Name: [bold]{self.item.title}[/bold]")
                yield Label(f"Group: {self.item.group_title or 'None'}")
                yield Label(f"URL: {self.item.url}")

            with Horizontal(id="confirm-buttons"):
                yield Button("Delete (Enter/Y)", variant="error", id="btn-delete")
                yield Button("Cancel (Esc/N)", variant="default", id="btn-cancel")

    def on_mount(self) -> None:
        # Focus Delete button so single Enter/Y confirms cleanly
        self.query_one("#btn-delete", Button).focus()

    @on(Button.Pressed, "#btn-delete")
    def on_delete(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#btn-cancel")
    def on_cancel(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class OpenFileModal(ModalScreen[Optional[str]]):
    """File open modal dialog."""

    DEFAULT_CSS = """
    OpenFileModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.65);
    }

    #open-box {
        width: 64;
        height: 15;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }

    #open-title {
        height: 1;
        color: $accent;
        text-style: bold;
        text-align: center;
        margin-bottom: 1;
    }

    #open-hint {
        height: 1;
        color: $text-muted;
    }

    #file-path-input {
        margin-bottom: 0;
    }

    #local-files-label {
        height: 1;
        color: $text-muted;
        margin-top: 0;
    }

    #open-buttons {
        height: 3;
        align: right middle;
        margin-top: 1;
    }

    #open-buttons Button {
        margin-left: 1;
        min-width: 10;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, current_file: str):
        super().__init__()
        self.current_file = current_file

    def compose(self) -> ComposeResult:
        local_files = [f for f in os.listdir(".") if f.lower().endswith((".m3u", ".m3u8"))]
        with Vertical(id="open-box"):
            yield Label("Open M3U File", id="open-title")
            yield Label("File path:", id="open-hint")
            yield Input(value=self.current_file, id="file-path-input")
            if local_files:
                yield Label("Found: " + "  ".join(local_files), id="local-files-label")
            with Horizontal(id="open-buttons"):
                yield Button("Open", variant="primary", id="btn-open")
                yield Button("Cancel", id="btn-cancel")

    def on_mount(self) -> None:
        self.query_one("#file-path-input", Input).focus()

    @on(Button.Pressed, "#btn-open")
    def on_open(self) -> None:
        path = self.query_one("#file-path-input", Input).value.strip()
        self.dismiss(path if path else None)

    @on(Input.Submitted)
    def on_submit(self) -> None:
        path = self.query_one("#file-path-input", Input).value.strip()
        self.dismiss(path if path else None)

    @on(Button.Pressed, "#btn-cancel")
    def on_cancel(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ExportJsonModal(ModalScreen[Optional[str]]):
    """Export channel groups to JSON modal."""

    DEFAULT_CSS = """
    ExportJsonModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.65);
    }

    #export-box {
        width: 64;
        height: 13;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }

    #export-title {
        height: 1;
        color: $accent;
        text-style: bold;
        text-align: center;
        margin-bottom: 1;
    }

    #export-hint {
        height: 1;
        color: $text-muted;
    }

    #export-path-input {
        margin-bottom: 0;
    }

    #export-buttons {
        height: 3;
        align: right middle;
        margin-top: 1;
    }

    #export-buttons Button {
        margin-left: 1;
        min-width: 10;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, default_filename: str):
        super().__init__()
        self.default_filename = default_filename

    def compose(self) -> ComposeResult:
        with Vertical(id="export-box"):
            yield Label("Export Groups to JSON", id="export-title")
            yield Label("Save path:", id="export-hint")
            yield Input(value=self.default_filename, id="export-path-input")
            with Horizontal(id="export-buttons"):
                yield Button("Export", variant="primary", id="btn-export")
                yield Button("Cancel", id="btn-cancel")

    def on_mount(self) -> None:
        self.query_one("#export-path-input", Input).focus()

    @on(Button.Pressed, "#btn-export")
    def on_export(self) -> None:
        path = self.query_one("#export-path-input", Input).value.strip()
        self.dismiss(path if path else None)

    @on(Input.Submitted)
    def on_submit(self) -> None:
        path = self.query_one("#export-path-input", Input).value.strip()
        self.dismiss(path if path else None)

    @on(Button.Pressed, "#btn-cancel")
    def on_cancel(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ImportJsonModal(ModalScreen[Optional[str]]):
    """Import channel groups from JSON modal."""

    DEFAULT_CSS = """
    ImportJsonModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.65);
    }

    #import-box {
        width: 64;
        height: 15;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }

    #import-title {
        height: 1;
        color: $accent;
        text-style: bold;
        text-align: center;
        margin-bottom: 1;
    }

    #import-hint {
        height: 1;
        color: $text-muted;
    }

    #import-path-input {
        margin-bottom: 0;
    }

    #local-json-label {
        height: 1;
        color: $text-muted;
        margin-top: 0;
    }

    #import-buttons {
        height: 3;
        align: right middle;
        margin-top: 1;
    }

    #import-buttons Button {
        margin-left: 1;
        min-width: 10;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, default_filename: str):
        super().__init__()
        self.default_filename = default_filename

    def compose(self) -> ComposeResult:
        local_files = [f for f in os.listdir(".") if f.lower().endswith(".json")]
        with Vertical(id="import-box"):
            yield Label("Import Groups from JSON", id="import-title")
            yield Label("Path (unknown channels -> 'Nincs'):", id="import-hint")
            yield Input(value=self.default_filename, id="import-path-input")
            if local_files:
                yield Label("Found: " + "  ".join(local_files), id="local-json-label")
            with Horizontal(id="import-buttons"):
                yield Button("Import", variant="primary", id="btn-import")
                yield Button("Cancel", id="btn-cancel")

    def on_mount(self) -> None:
        self.query_one("#import-path-input", Input).focus()

    @on(Button.Pressed, "#btn-import")
    def on_import(self) -> None:
        path = self.query_one("#import-path-input", Input).value.strip()
        self.dismiss(path if path else None)

    @on(Input.Submitted)
    def on_submit(self) -> None:
        path = self.query_one("#import-path-input", Input).value.strip()
        self.dismiss(path if path else None)

    @on(Button.Pressed, "#btn-cancel")
    def on_cancel(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# -----------------------------------------------------------------------------
# Main Application
# -----------------------------------------------------------------------------

class M3UEditorApp(App):
    """M3U TUI Editor Main Application."""

    TITLE = "M3U TUI Editor"
    SUB_TITLE = "Visual Playlist Editor"

    CSS = """
    Screen {
        background: $background;
        color: $text;
    }

    /* Top Toolbar */
    #toolbar {
        height: auto;
        background: $panel;
        padding: 0 1;
        border-bottom: solid $primary-muted;
        align-vertical: middle;
    }

    #filter-bar {
        height: auto;
        padding: 1 0 0 0;
        align-vertical: middle;
    }

    .filter-item {
        height: auto;
        align-vertical: middle;
        margin-right: 2;
    }

    .filter-label {
        color: $accent;
        text-style: bold;
        margin-right: 1;
        padding-top: 1;
    }

    #group-select {
        width: 30;
    }

    #search-input {
        width: 28;
    }

    #btn-clear-filters {
        min-width: 14;
        margin-left: 1;
    }

    #action-bar {
        height: auto;
        padding: 0 0 1 0;
        align-vertical: middle;
    }

    #action-bar Button {
        margin-right: 1;
        height: 3;
    }

    /* Main Table Container */
    #table-container {
        height: 1fr;
        padding: 0 1;
    }

    DataTable {
        height: 100%;
        background: $surface;
        border: solid $accent-muted;
    }

    DataTable > .datatable--cursor {
        background: $primary;
        color: $text;
        text-style: bold;
    }

    /* Status Bar */
    #status-bar {
        height: 1;
        background: $panel-darken-1;
        color: $text-muted;
        padding: 0 1;
        align-vertical: middle;
        border-top: solid $panel;
    }

    #status-file {
        width: auto;
        text-style: bold;
        color: $accent;
        margin-right: 2;
    }

    #status-dirty {
        width: auto;
        margin-right: 2;
    }

    #status-counts {
        width: 1fr;
    }

    #status-selected {
        width: auto;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("enter", "edit_item", "Edit", show=True, priority=False),
        Binding("space", "toggle_select", "Select", show=True, priority=False),
        Binding("c", "check_streams", "Check Streams", show=True, priority=False),
        Binding("g", "batch_edit_group", "Set Group", show=True, priority=False),
        Binding("ctrl+e", "export_json", "Export JSON", show=True, priority=False),
        Binding("ctrl+i", "import_json", "Import JSON", show=True, priority=False),
        Binding("ctrl+a", "select_all", "Select All", show=True, priority=False),
        Binding("ctrl+d", "clear_selection", "Deselect", show=True, priority=False),
        Binding("insert", "add_item", "New", show=True, priority=False),
        Binding("delete", "delete_item", "Delete", show=True, priority=False),
        Binding("ctrl+s", "save_file", "Save", show=True, priority=False),
        Binding("ctrl+o", "open_file", "Open", show=True, priority=False),
        Binding("ctrl+f", "focus_group_filter", "Group Filter", show=True, priority=False),
        Binding("slash", "focus_search", "Search", show=True, priority=False),
        Binding("escape", "clear_filters", "Clear Filters", show=True, priority=False),
        Binding("q", "quit_app", "Quit", show=True),
    ]

    COLUMNS = [
        ("col_sel", "Sel"),
        ("col_num", "#"),
        ("col_status", "Status"),
        ("col_grp", "Group"),
        ("col_name", "Channel Name"),
        ("col_epg", "EPG ID"),
        ("col_logo", "Logo"),
        ("col_url", "Stream URL"),
    ]

    def __init__(self, file_path: str = "hun_filtered-epg.m3u"):
        super().__init__()
        self.file_path = file_path
        self.header_line = "#EXTM3U"
        self.items: List[M3UItem] = []
        self.is_dirty: bool = False
        self.selected_group: str = "ALL"
        self.search_term: str = ""
        self.filtered_indices: List[int] = []
        self.selected_indices: Set[int] = set()
        self.sort_column: str = "col_num"
        self.sort_reverse: bool = False
        self._delete_modal_active: bool = False
        # Stream check statuses: item_idx -> (status_type, elapsed_ms, label_str)
        self.stream_statuses: Dict[int, Tuple[str, float, str]] = {}
        self._is_checking_streams: bool = False
        self._cancel_check: bool = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        with Vertical(id="toolbar"):
            with Horizontal(id="filter-bar"):
                with Horizontal(classes="filter-item"):
                    yield Label("Group:", classes="filter-label")
                    yield Select(
                        options=[("All Groups", "ALL")],
                        value="ALL",
                        id="group-select",
                        allow_blank=False
                    )

                with Horizontal(classes="filter-item"):
                    yield Label("Search (/):", classes="filter-label")
                    yield Input(
                        placeholder="Filter name, URL, EPG...",
                        id="search-input"
                    )

                yield Button("Clear (Esc)", id="btn-clear-filters", variant="default")

            with Horizontal(id="action-bar"):
                yield Button("+ New (Ins)", id="btn-add", variant="success")
                yield Button("Edit (Enter)", id="btn-edit", variant="primary")
                yield Button("Check (C)", id="btn-check-streams", variant="warning")
                yield Button("Set Group (G)", id="btn-batch-group", variant="default")
                yield Button("Export JSON", id="btn-export-json", variant="default")
                yield Button("Import JSON", id="btn-import-json", variant="default")
                yield Button("Select All", id="btn-select-all", variant="default")
                yield Button("Deselect", id="btn-clear-selection", variant="default")
                yield Button("Delete (Del)", id="btn-delete", variant="error")
                yield Button("Save (Ctrl+S)", id="toolbar-btn-save", variant="default")
                yield Button("Open (Ctrl+O)", id="toolbar-btn-open", variant="default")

        with Container(id="table-container"):
            yield DataTable(id="channels-table", cursor_type="row", zebra_stripes=True)

        with Horizontal(id="status-bar"):
            yield Label("", id="status-file")
            yield Label("", id="status-dirty")
            yield Label("", id="status-counts")
            yield Label("", id="status-selected")

        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#channels-table", DataTable)
        for col_key, label in self.COLUMNS:
            arrow = ""
            if self.sort_column == col_key:
                arrow = " ▼" if self.sort_reverse else " ▲"
            table.add_column(f"{label}{arrow}", key=col_key)

        if os.path.exists(self.file_path):
            self.load_file(self.file_path)
        else:
            local_m3us = [f for f in os.listdir(".") if f.lower().endswith((".m3u", ".m3u8"))]
            if local_m3us:
                self.load_file(local_m3us[0])
            else:
                self.notify("No M3U file found. Use Open (Ctrl+O) to load one!", severity="warning")
                self.update_status_bar()

        table.focus()

    # -------------------------------------------------------------------------
    # File Operations
    # -------------------------------------------------------------------------

    def load_file(self, path: str) -> None:
        try:
            self.header_line, self.items = load_m3u(path)
            self.file_path = path
            self.is_dirty = False
            self.selected_group = "ALL"
            self.search_term = ""
            self.selected_indices.clear()

            self.query_one("#search-input", Input).value = ""
            self.update_group_select_options()
            self.refresh_table()
            self.notify(f"Loaded: {os.path.basename(path)} ({len(self.items)} channels)", severity="information")
        except Exception as e:
            self.notify(f"Load error: {e}", severity="error")

    def save_current_file(self) -> None:
        try:
            save_m3u(self.file_path, self.header_line, self.items)
            self.is_dirty = False
            self.update_status_bar()
            self.notify(f"Saved: {os.path.basename(self.file_path)} ({len(self.items)} channels)", severity="information")
        except Exception as e:
            self.notify(f"Save error: {e}", severity="error")

    # -------------------------------------------------------------------------
    # Filter & Group Handling
    # -------------------------------------------------------------------------

    def update_group_select_options(self) -> None:
        group_counts: Dict[str, int] = {}
        for item in self.items:
            g = item.group_title.strip() if item.group_title else "None"
            group_counts[g] = group_counts.get(g, 0) + 1

        options = [("All Groups (" + str(len(self.items)) + ")", "ALL")]
        for g in sorted(group_counts.keys(), key=lambda s: s.lower()):
            options.append((f"{g} ({group_counts[g]})", g))

        select = self.query_one("#group-select", Select)
        select.set_options(options)
        if self.selected_group in [val for _, val in options]:
            select.value = self.selected_group
        else:
            self.selected_group = "ALL"
            select.value = "ALL"

    @on(Select.Changed, "#group-select")
    def on_group_select_changed(self, event: Select.Changed) -> None:
        self.selected_group = str(event.value) if event.value is not Select.BLANK else "ALL"
        self.refresh_table()

    @on(Input.Changed, "#search-input")
    def on_search_changed(self, event: Input.Changed) -> None:
        self.search_term = event.value.strip().lower()
        self.refresh_table()

    @on(Button.Pressed, "#btn-clear-filters")
    def on_clear_filters_pressed(self) -> None:
        self.action_clear_filters()

    def action_clear_filters(self) -> None:
        self.selected_group = "ALL"
        self.search_term = ""
        self.query_one("#group-select", Select).value = "ALL"
        self.query_one("#search-input", Input).value = ""
        self.refresh_table()
        self.query_one("#channels-table", DataTable).focus()
        self.notify("Filters cleared.", severity="information")

    def action_focus_group_filter(self) -> None:
        self.query_one("#group-select", Select).focus()

    def action_focus_search(self) -> None:
        self.query_one("#search-input", Input).focus()

    # -------------------------------------------------------------------------
    # Selection Handling (Multi-select for batch actions)
    # -------------------------------------------------------------------------

    def action_toggle_select(self) -> None:
        """Toggle selection on the current row with Space and step down."""
        table = self.query_one("#channels-table", DataTable)
        if not self.filtered_indices:
            return

        cursor_row = table.cursor_row
        if 0 <= cursor_row < len(self.filtered_indices):
            item_idx = self.filtered_indices[cursor_row]
            if item_idx in self.selected_indices:
                self.selected_indices.remove(item_idx)
            else:
                self.selected_indices.add(item_idx)

            self.refresh_table(preserve_cursor=True)

            if cursor_row < len(self.filtered_indices) - 1:
                table.action_cursor_down()

            self.update_status_bar()

    @on(Button.Pressed, "#btn-select-all")
    def on_btn_select_all(self) -> None:
        self.action_select_all()

    def action_select_all(self) -> None:
        """Select all currently visible / filtered channels."""
        if not self.filtered_indices:
            return
        for idx in self.filtered_indices:
            self.selected_indices.add(idx)
        self.refresh_table(preserve_cursor=True)
        self.update_status_bar()
        self.notify(f"{len(self.filtered_indices)} channels selected.", severity="information")
        self.query_one("#channels-table", DataTable).focus()

    @on(Button.Pressed, "#btn-clear-selection")
    def on_btn_clear_selection(self) -> None:
        self.action_clear_selection()

    def action_clear_selection(self) -> None:
        """Clear all active selections."""
        count = len(self.selected_indices)
        self.selected_indices.clear()
        self.refresh_table(preserve_cursor=True)
        self.update_status_bar()
        self.notify(f"Cleared {count} selections.", severity="information")
        self.query_one("#channels-table", DataTable).focus()

    # -------------------------------------------------------------------------
    # Column Sorting & Table Refresh
    # -------------------------------------------------------------------------

    def get_sort_key_fn(self, col_key: str):
        """Return the sort key callable for the chosen column."""
        if col_key == "col_sel":
            return lambda idx: (0 if idx in self.selected_indices else 1, (self.items[idx].title or "").lower())
        elif col_key == "col_num":
            return lambda idx: idx
        elif col_key == "col_status":
            status_priority = {"FAST": 0, "SLOW": 1, "CHECKING": 2, "OFFLINE": 3}
            return lambda idx: (
                status_priority.get(self.stream_statuses.get(idx, ("", 0, ""))[0], 4),
                self.stream_statuses.get(idx, ("", 999999, ""))[1],
                (self.items[idx].title or "").lower()
            )
        elif col_key == "col_grp":
            return lambda idx: ((self.items[idx].group_title or "").lower(), (self.items[idx].title or "").lower())
        elif col_key == "col_name":
            return lambda idx: (self.items[idx].title or "").lower()
        elif col_key == "col_epg":
            return lambda idx: ((self.items[idx].tvg_id or "").lower(), (self.items[idx].title or "").lower())
        elif col_key == "col_logo":
            return lambda idx: (0 if self.items[idx].tvg_logo else 1, (self.items[idx].title or "").lower())
        elif col_key == "col_url":
            return lambda idx: ((self.items[idx].url or "").lower(), (self.items[idx].title or "").lower())
        return lambda idx: idx

    def update_column_headers(self) -> None:
        """Update header labels with direction arrows."""
        table = self.query_one("#channels-table", DataTable)
        for col_key, base_title in self.COLUMNS:
            arrow = ""
            if self.sort_column == col_key:
                arrow = " ▼" if self.sort_reverse else " ▲"
            try:
                col = table.get_column(col_key)
                col.label = Text(f"{base_title}{arrow}")
            except Exception:
                pass
        table.refresh()

    @on(DataTable.HeaderSelected, "#channels-table")
    def on_header_selected(self, event: DataTable.HeaderSelected) -> None:
        """Sort by clicked column header."""
        col_key = str(event.column_key.value)
        if self.sort_column == col_key:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = col_key
            self.sort_reverse = False

        self.update_column_headers()
        self.refresh_table(preserve_cursor=True)

        col_names = {
            "col_sel": "Select",
            "col_num": "# (Default)",
            "col_status": "Status",
            "col_grp": "Group",
            "col_name": "Channel Name",
            "col_epg": "EPG ID",
            "col_logo": "Logo",
            "col_url": "Stream URL",
        }
        name = col_names.get(col_key, col_key)
        direction = "desc ▼" if self.sort_reverse else "asc ▲"
        self.notify(f"Sorted: {name} ({direction})", severity="information")

    def refresh_table(self, keep_selection_idx: Optional[int] = None, preserve_cursor: bool = False) -> None:
        table = self.query_one("#channels-table", DataTable)
        old_cursor_row = table.cursor_row
        table.clear()

        # 1. Filter
        matching_indices: List[int] = []
        for idx, item in enumerate(self.items):
            if self.selected_group != "ALL":
                item_group = item.group_title.strip() if item.group_title else "None"
                if item_group != self.selected_group:
                    continue

            if self.search_term:
                full_searchable = f"{item.title} {item.group_title} {item.tvg_id} {item.url}".lower()
                if self.search_term not in full_searchable:
                    continue

            matching_indices.append(idx)

        # 2. Sort
        if self.sort_column:
            sort_fn = self.get_sort_key_fn(self.sort_column)
            matching_indices.sort(key=sort_fn, reverse=self.sort_reverse)

        self.filtered_indices = matching_indices

        target_row_to_highlight = None

        for display_idx, idx in enumerate(self.filtered_indices, 1):
            item = self.items[idx]
            is_sel = idx in self.selected_indices
            sel_mark = Text("[x]", style="bold green") if is_sel else Text("[ ]", style="dim")

            # Stream status display and color coding
            stream_info = self.stream_statuses.get(idx)
            if stream_info:
                st_type, elapsed_ms, st_label = stream_info
                if st_type == "FAST":
                    status_text = Text(st_label, style="bold green")
                    name_style = "bold green"
                elif st_type == "SLOW":
                    status_text = Text(st_label, style="bold yellow")
                    name_style = "bold yellow"
                elif st_type == "CHECKING":
                    status_text = Text("Checking...", style="cyan")
                    name_style = "bold cyan"
                else:
                    status_text = Text(st_label, style="bold red")
                    name_style = "bold red"
            else:
                status_text = Text("-", style="dim")
                name_style = "bold" if not is_sel else "bold yellow"

            name_text = Text(item.title, style=name_style)
            logo_mark = "Yes" if item.tvg_logo else "-"
            group_disp = item.group_title if item.group_title else "[dim]-[/dim]"
            tvg_id_disp = item.tvg_id if item.tvg_id else "[dim]-[/dim]"

            row_key = str(idx)
            table.add_row(
                sel_mark,
                str(display_idx),
                status_text,
                group_disp,
                name_text,
                tvg_id_disp,
                logo_mark,
                item.url,
                key=row_key
            )

            if keep_selection_idx is not None and idx == keep_selection_idx:
                target_row_to_highlight = display_idx - 1

        self.update_status_bar()

        # Restore cursor position
        if len(self.filtered_indices) > 0:
            if target_row_to_highlight is not None:
                table.move_cursor(row=target_row_to_highlight)
            elif preserve_cursor and 0 <= old_cursor_row < len(self.filtered_indices):
                table.move_cursor(row=old_cursor_row)
            elif table.cursor_row >= len(self.filtered_indices):
                table.move_cursor(row=len(self.filtered_indices) - 1)

    def get_selected_item_index(self) -> Optional[int]:
        """Return the items list index of the currently highlighted row."""
        table = self.query_one("#channels-table", DataTable)
        if not self.filtered_indices:
            return None
        cursor_row = table.cursor_row
        if 0 <= cursor_row < len(self.filtered_indices):
            return self.filtered_indices[cursor_row]
        return None

    def update_status_bar(self, checking_info: Optional[str] = None) -> None:
        filename = os.path.basename(self.file_path) if self.file_path else "No file"
        self.query_one("#status-file", Label).update(f"File: {filename}")

        dirty_label = self.query_one("#status-dirty", Label)
        if self.is_dirty:
            dirty_label.update("[bold yellow]* Modified[/bold yellow]")
        else:
            dirty_label.update("[bold green]Saved[/bold green]")

        total_cnt = len(self.items)
        filtered_cnt = len(self.filtered_indices)
        sel_count = len(self.selected_indices)

        col_names = {
            "col_sel": "Select",
            "col_num": "# (Default)",
            "col_status": "Status",
            "col_grp": "Group",
            "col_name": "Name",
            "col_epg": "EPG",
            "col_logo": "Logo",
            "col_url": "URL",
        }
        sort_name = col_names.get(self.sort_column, self.sort_column)
        sort_arrow = "▼" if self.sort_reverse else "▲"
        sort_text = f" | [cyan]Sorted: {sort_name} {sort_arrow}[/cyan]"

        sel_text = f" | [bold green]Selected: {sel_count}[/bold green]" if sel_count > 0 else ""
        grp_text = f" | Group: {self.selected_group}" if self.selected_group != "ALL" else ""
        chk_text = f" | {checking_info}" if checking_info else ""

        self.query_one("#status-counts", Label).update(
            f"Total: {total_cnt} | Shown: {filtered_cnt}{grp_text}{sort_text}{chk_text}{sel_text}"
        )

        selected_idx = self.get_selected_item_index()
        selected_label = self.query_one("#status-selected", Label)
        if selected_idx is not None and 0 <= selected_idx < len(self.items):
            sel_item = self.items[selected_idx]
            selected_label.update(f"Current: [bold]{sel_item.title}[/bold]")
        else:
            selected_label.update("")

        batch_btn = self.query_one("#btn-batch-group", Button)
        if sel_count > 0:
            batch_btn.label = f"Set Group ({sel_count})"
        else:
            batch_btn.label = "Set Group (G)"

    @on(DataTable.RowHighlighted, "#channels-table")
    def on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.update_status_bar()

    # -------------------------------------------------------------------------
    # Actions: Edit (Enter), Batch Group (G), Add (Ins), Delete (Del)
    # -------------------------------------------------------------------------

    @on(DataTable.RowSelected, "#channels-table")
    def on_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_edit_item()

    @on(Button.Pressed, "#btn-edit")
    def on_btn_edit(self) -> None:
        self.action_edit_item()

    def action_edit_item(self) -> None:
        selected_idx = self.get_selected_item_index()
        if selected_idx is None:
            self.notify("No channel selected to edit!", severity="warning")
            return

        item = self.items[selected_idx]
        def on_edit_saved(updated_item: Optional[M3UItem]) -> None:
            if updated_item is not None:
                self.items[selected_idx] = updated_item
                self.is_dirty = True
                self.update_group_select_options()
                self.refresh_table(keep_selection_idx=selected_idx)
                self.notify(f"Updated: {updated_item.title}", severity="information")
            self.query_one("#channels-table", DataTable).focus()

        self.push_screen(ItemEditorModal(item=item, is_new=False), callback=on_edit_saved)

    @on(Button.Pressed, "#btn-batch-group")
    def on_btn_batch_group(self) -> None:
        self.action_batch_edit_group()

    def action_batch_edit_group(self) -> None:
        """Batch modify group-title for all selected channels."""
        target_indices = list(self.selected_indices)

        if not target_indices:
            sel_idx = self.get_selected_item_index()
            if sel_idx is not None:
                target_indices = [sel_idx]
            else:
                self.notify(
                    "Select one or more channels with [Space] to batch modify groups!",
                    severity="warning"
                )
                return

        existing_groups = list({item.group_title for item in self.items if item.group_title})

        def on_batch_saved(new_group: Optional[str]) -> None:
            if new_group is not None:
                count = len(target_indices)
                for idx in target_indices:
                    self.items[idx].group_title = new_group
                self.is_dirty = True
                self.selected_indices.clear()
                self.update_group_select_options()
                self.refresh_table()
                self.notify(f"Updated group for {count} channels: '{new_group}'", severity="information")
            self.query_one("#channels-table", DataTable).focus()

        self.push_screen(
            BatchGroupModal(selected_count=len(target_indices), existing_groups=existing_groups),
            callback=on_batch_saved
        )

    @on(Button.Pressed, "#btn-add")
    def on_btn_add(self) -> None:
        self.action_add_item()

    def action_add_item(self) -> None:
        initial_group = self.selected_group if self.selected_group != "ALL" and self.selected_group != "None" else ""

        def on_add_saved(new_item: Optional[M3UItem]) -> None:
            if new_item is not None:
                sel_idx = self.get_selected_item_index()
                insert_pos = (sel_idx + 1) if sel_idx is not None else len(self.items)
                self.items.insert(insert_pos, new_item)
                self.is_dirty = True
                self.update_group_select_options()
                self.refresh_table(keep_selection_idx=insert_pos)
                self.notify(f"Added: {new_item.title}", severity="information")
            self.query_one("#channels-table", DataTable).focus()

        self.push_screen(ItemEditorModal(item=None, initial_group=initial_group, is_new=True), callback=on_add_saved)

    @on(Button.Pressed, "#btn-delete")
    def on_btn_delete(self) -> None:
        self.action_delete_item()

    def action_delete_item(self) -> None:
        # Prevent double-triggering or chaining delete modals
        if getattr(self, "_delete_modal_active", False):
            return

        selected_idx = self.get_selected_item_index()
        if selected_idx is None:
            self.notify("No channel selected to delete!", severity="warning")
            return

        item_to_delete = self.items[selected_idx]
        self._delete_modal_active = True

        def on_delete_confirmed(confirmed: bool) -> None:
            self._delete_modal_active = False
            if confirmed:
                deleted_title = item_to_delete.title
                if selected_idx in self.selected_indices:
                    self.selected_indices.remove(selected_idx)
                del self.items[selected_idx]
                self.is_dirty = True
                self.update_group_select_options()

                # Place cursor on next row or end of list
                next_sel = selected_idx if selected_idx < len(self.items) else (len(self.items) - 1)
                self.refresh_table(keep_selection_idx=next_sel if next_sel >= 0 else None)
                self.notify(f"Deleted: {deleted_title}", severity="information")

            # Always return focus directly to the table
            self.query_one("#channels-table", DataTable).focus()

        self.push_screen(ConfirmDeleteModal(item_to_delete), callback=on_delete_confirmed)

    @on(Button.Pressed, "#toolbar-btn-save")
    def on_btn_save(self) -> None:
        self.action_save_file()

    def action_save_file(self) -> None:
        self.save_current_file()

    @on(Button.Pressed, "#toolbar-btn-open")
    def on_btn_open(self) -> None:
        self.action_open_file()

    def action_open_file(self) -> None:
        def on_open_path(path: Optional[str]) -> None:
            if path:
                if os.path.exists(path):
                    self.load_file(path)
                else:
                    self.notify(f"File not found: {path}", severity="error")
            self.query_one("#channels-table", DataTable).focus()

        self.push_screen(OpenFileModal(self.file_path), callback=on_open_path)

    # -------------------------------------------------------------------------
    # JSON Group Export / Import
    # -------------------------------------------------------------------------

    def _default_json_path(self) -> str:
        """Derive default JSON path from current m3u file path."""
        base = os.path.splitext(self.file_path)[0]
        return base + "_groups.json"

    @on(Button.Pressed, "#btn-export-json")
    def on_btn_export_json(self) -> None:
        self.action_export_json()

    def action_export_json(self) -> None:
        def on_path_chosen(path: Optional[str]) -> None:
            if path:
                try:
                    mapping = {item.title: item.group_title for item in self.items}
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(mapping, f, ensure_ascii=False, indent=2, sort_keys=True)
                    self.notify(
                        f"Exported {len(mapping)} channels to: {os.path.basename(path)}",
                        severity="information",
                    )
                except Exception as e:
                    self.notify(f"Export error: {e}", severity="error")
            self.query_one("#channels-table", DataTable).focus()

        self.push_screen(ExportJsonModal(self._default_json_path()), callback=on_path_chosen)

    @on(Button.Pressed, "#btn-import-json")
    def on_btn_import_json(self) -> None:
        self.action_import_json()

    def action_import_json(self) -> None:
        def on_path_chosen(path: Optional[str]) -> None:
            if path:
                if not os.path.exists(path):
                    self.notify(f"File not found: {os.path.basename(path)}", severity="error")
                    self.query_one("#channels-table", DataTable).focus()
                    return
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        mapping: Dict[str, str] = json.load(f)
                    updated = 0
                    nincs = 0
                    for item in self.items:
                        if item.title in mapping:
                            item.group_title = mapping[item.title]
                            updated += 1
                        else:
                            item.group_title = "Nincs"
                            nincs += 1
                    self.is_dirty = True
                    self.update_group_select_options()
                    self.refresh_table(preserve_cursor=True)
                    self.notify(
                        f"Import done: {updated} updated, {nincs} set to 'Nincs'.",
                        severity="information",
                    )
                except Exception as e:
                    self.notify(f"Import error: {e}", severity="error")
            self.query_one("#channels-table", DataTable).focus()

        self.push_screen(ImportJsonModal(self._default_json_path()), callback=on_path_chosen)

    # -------------------------------------------------------------------------
    # Stream Availability Checker (Multi-threaded)
    # -------------------------------------------------------------------------

    def update_stream_cell(self, idx: int, status_type: str, elapsed_ms: float, label_str: str) -> None:
        table = self.query_one("#channels-table", DataTable)
        row_key = str(idx)
        try:
            if status_type == "FAST":
                status_text = Text(label_str, style="bold green")
                name_style = "bold green"
            elif status_type == "SLOW":
                status_text = Text(label_str, style="bold yellow")
                name_style = "bold yellow"
            elif status_type == "CHECKING":
                status_text = Text("Checking...", style="cyan")
                name_style = "bold cyan"
            else:
                status_text = Text(label_str, style="bold red")
                name_style = "bold red"

            name_text = Text(self.items[idx].title, style=name_style)
            table.update_cell(row_key=row_key, column_key="col_status", value=status_text)
            table.update_cell(row_key=row_key, column_key="col_name", value=name_text)
        except Exception:
            pass

    @work(thread=True)
    def run_stream_checker(self, target_indices: List[int]) -> None:
        total = len(target_indices)
        completed = 0
        fast_cnt = 0
        slow_cnt = 0
        offline_cnt = 0

        self._is_checking_streams = True
        self._cancel_check = False

        # Mark all target channels as CHECKING
        for idx in target_indices:
            self.stream_statuses[idx] = ("CHECKING", 0.0, "Checking...")
            self.call_from_thread(self.update_stream_cell, idx, "CHECKING", 0.0, "Checking...")

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            future_to_idx = {
                executor.submit(check_single_stream, self.items[idx].url): idx
                for idx in target_indices
            }

            for future in concurrent.futures.as_completed(future_to_idx):
                if self._cancel_check:
                    executor.shutdown(wait=False, cancel_futures=True)
                    break

                idx = future_to_idx[future]
                try:
                    status_type, elapsed_ms, label_str = future.result()
                except Exception:
                    status_type, elapsed_ms, label_str = "OFFLINE", 0.0, "Error"

                self.stream_statuses[idx] = (status_type, elapsed_ms, label_str)

                if status_type == "FAST":
                    fast_cnt += 1
                elif status_type == "SLOW":
                    slow_cnt += 1
                else:
                    offline_cnt += 1

                completed += 1
                self.call_from_thread(
                    self.handle_stream_progress,
                    idx, status_type, elapsed_ms, label_str,
                    completed, total, fast_cnt, slow_cnt, offline_cnt
                )

        self._is_checking_streams = False
        self.call_from_thread(
            self.handle_check_completed,
            completed, total, fast_cnt, slow_cnt, offline_cnt
        )

    def handle_stream_progress(
        self,
        idx: int,
        status_type: str,
        elapsed_ms: float,
        label_str: str,
        completed: int,
        total: int,
        fast_cnt: int,
        slow_cnt: int,
        offline_cnt: int
    ) -> None:
        self.update_stream_cell(idx, status_type, elapsed_ms, label_str)
        btn = self.query_one("#btn-check-streams", Button)
        btn.label = f"Stop ({completed}/{total})"
        self.update_status_bar(
            checking_info=f"[bold green]Fast: {fast_cnt}[/bold green] | [bold yellow]Slow: {slow_cnt}[/bold yellow] | [bold red]Offline: {offline_cnt}[/bold red] ({completed}/{total})"
        )

    def handle_check_completed(
        self,
        completed: int,
        total: int,
        fast_cnt: int,
        slow_cnt: int,
        offline_cnt: int
    ) -> None:
        btn = self.query_one("#btn-check-streams", Button)
        btn.label = "Check (C)"
        self.update_status_bar()
        self.notify(
            f"Check done: {completed}/{total} streams. Fast: {fast_cnt}, Slow: {slow_cnt}, Offline: {offline_cnt}",
            severity="information"
        )

    @on(Button.Pressed, "#btn-check-streams")
    def on_btn_check_streams(self) -> None:
        self.action_check_streams()

    def action_check_streams(self) -> None:
        """Start or stop concurrent checking of streams."""
        if getattr(self, "_is_checking_streams", False):
            self._cancel_check = True
            self.notify("Stopping stream checks...", severity="warning")
            btn = self.query_one("#btn-check-streams", Button)
            btn.label = "Stopping..."
            return

        if self.selected_indices:
            target_indices = [idx for idx in self.filtered_indices if idx in self.selected_indices]
            if not target_indices:
                target_indices = list(self.selected_indices)
            desc = f"{len(target_indices)} selected"
        else:
            target_indices = list(self.filtered_indices)
            desc = f"{len(target_indices)} visible"

        if not target_indices:
            self.notify("No channels to check!", severity="warning")
            return

        btn = self.query_one("#btn-check-streams", Button)
        btn.label = f"Stop (0/{len(target_indices)})"
        self.notify(f"Checking {desc} streams (multi-threaded)...", severity="information")
        self.run_stream_checker(target_indices)

    def action_quit_app(self) -> None:
        if self.is_dirty:
            class ConfirmExitModal(ModalScreen[bool]):
                DEFAULT_CSS = """
                ConfirmExitModal {
                    align: center middle;
                    background: rgba(0, 0, 0, 0.7);
                }
                #exit-box {
                    width: 58;
                    height: auto;
                    border: heavy $warning;
                    background: $surface;
                    padding: 1 2;
                }
                #exit-title {
                    color: $warning;
                    text-style: bold;
                    margin-bottom: 1;
                    text-align: center;
                }
                #exit-buttons {
                    margin-top: 1;
                    align: right middle;
                }
                #exit-buttons Button {
                    margin-left: 2;
                }
                """
                BINDINGS = [Binding("escape", "cancel", "Cancel")]

                def compose(self) -> ComposeResult:
                    with Vertical(id="exit-box"):
                        yield Label("UNSAVED CHANGES", id="exit-title")
                        yield Label("You have unsaved changes. Exit without saving?")
                        with Horizontal(id="exit-buttons"):
                            yield Button("Save & Exit", variant="primary", id="btn-save-exit")
                            yield Button("Discard & Exit", variant="error", id="btn-discard-exit")
                            yield Button("Cancel", variant="default", id="btn-cancel")

                @on(Button.Pressed, "#btn-save-exit")
                def on_save_exit(self) -> None:
                    self.dismiss("SAVE_EXIT")

                @on(Button.Pressed, "#btn-discard-exit")
                def on_discard_exit(self) -> None:
                    self.dismiss("DISCARD_EXIT")

                @on(Button.Pressed, "#btn-cancel")
                def on_cancel(self) -> None:
                    self.dismiss("CANCEL")

                def action_cancel(self) -> None:
                    self.dismiss("CANCEL")

            def handle_exit_choice(choice: str) -> None:
                if choice == "SAVE_EXIT":
                    self.save_current_file()
                    self.exit()
                elif choice == "DISCARD_EXIT":
                    self.exit()

            self.push_screen(ConfirmExitModal(), callback=handle_exit_choice)
        else:
            self.exit()


# -----------------------------------------------------------------------------
# Entry Point
# -----------------------------------------------------------------------------

def main() -> None:
    file_path = "hun_filtered-epg.m3u"
    if len(sys.argv) > 1:
        file_path = sys.argv[1]

    app = M3UEditorApp(file_path=file_path)
    app.run()


if __name__ == "__main__":
    main()
