# M3U TUI Editor

A clean, keyboard-driven terminal user interface for browsing and editing M3U playlist files. Built with Python and [Textual](https://github.com/Textualize/textual).

![Python](https://img.shields.io/badge/Python-3.11%2B-blue) ![Textual](https://img.shields.io/badge/Textual-8.x-purple) ![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey)

---
## EPG for hun_filtered-epg.m3u:
```
https://kizman.net/guide/guide.xml
https://waterwave.synology.me/guide.xml
```
---
## Features

| Feature | Description |
|---------|-------------|
| 📂 **Open / Save** | Load and save `.m3u` / `.m3u8` files with full UTF-8 support |
| 📋 **Table view** | Sortable columns: `#`, `Name`, `Group`, `TVG-ID`, `URL`, `Status` |
| 🔎 **Filter & Search** | Filter by `group-title` via dropdown; live full-text search across all fields |
| ✏️ **Edit channel** | Edit all fields of a channel in a modal form (`Enter`) |
| ➕ **Add channel** | Insert a new channel entry (`Ins`) |
| 🗑️ **Delete channel** | Delete with confirmation dialog (`Del`) |
| ☑️ **Multi-select** | Select individual rows with `Space`; select all with `Ctrl+A` |
| 🏷️ **Batch group rename** | Change `group-title` for all selected channels at once (`G`) |
| 🌐 **Stream checker** | Multi-threaded availability check with green / yellow / red color coding (`C`) |
| 📤 **Export JSON** | Save `{channel name: group}` mapping to a JSON file (`Ctrl+E`) |
| 📥 **Import JSON** | Load group assignments from JSON; unknown channels get `"Nincs"` (`Ctrl+I`) |

---

## Requirements

```
python >= 3.11
textual >= 0.80
requests
```

Install dependencies:

```bash
pip install textual requests
```

---

## Usage

```bash
python app.py [file.m3u]
```

If no file is passed on the command line, use `Ctrl+O` inside the app to open one.

---

## Keyboard Shortcuts

### Navigation & Editing

| Key | Action |
|-----|--------|
| `↑` / `↓` | Move between channels |
| `Enter` | Edit selected channel |
| `Ins` | Add new channel |
| `Del` | Delete selected channel (with confirmation) |
| `Space` | Toggle row selection |
| `G` | Batch rename `group-title` for selected channels |

### Filtering & Search

| Key | Action |
|-----|--------|
| `Ctrl+F` | Focus the Group filter dropdown |
| `/` | Focus the search box |
| `Esc` | Clear all filters / close modals |

### File Operations

| Key | Action |
|-----|--------|
| `Ctrl+O` | Open file dialog |
| `Ctrl+S` | Save current file |
| `Ctrl+E` | Export `{channel: group}` mapping to JSON |
| `Ctrl+I` | Import group assignments from JSON |

### Selection

| Key | Action |
|-----|--------|
| `Ctrl+A` | Select all visible channels |
| `Ctrl+D` | Deselect all |

### Other

| Key | Action |
|-----|--------|
| `C` | Run stream availability check |
| `Q` | Quit (prompts to save if unsaved changes exist) |

---

## Column Sorting

Click any **column header** to sort by that column. Click again to reverse the sort order. An arrow (▲ / ▼) indicates the active sort column and direction.

---

## Stream Checker

Press `C` or click the **Check** button to test whether each stream URL is reachable.  
Checks run in parallel (up to 10 threads). Channels are colored based on response time:

| Color | Meaning |
|-------|---------|
| 🟢 Green | Fast response (< 2 s) |
| 🟡 Yellow | Slow response (2 – 3.5 s) |
| 🔴 Red | No response / timeout |

Press **Stop** to cancel an in-progress check.

---

## JSON Group Export / Import

### Export

`Ctrl+E` → saves a JSON file in the format:

```json
{
  "Channel Name A": "Group Title 1",
  "Channel Name B": "Group Title 2"
}
```

The default filename is derived from the loaded `.m3u` file (e.g. `playlist_groups.json`).

### Import

`Ctrl+I` → reads the JSON file and updates `group-title` for every channel:

- Channels **found** in the JSON → assigned the stored group
- Channels **not found** → assigned `"Nincs"`

This is useful for migrating group assignments between different playlist versions.

---

## M3U Format Support

The editor reads and writes standard `#EXTM3U` / `#EXTINF` playlists:

```
#EXTM3U
#EXTINF:-1 tvg-id="..." tvg-logo="..." group-title="News",Channel Name
http://stream.example.com/live
```

All known attributes (`tvg-id`, `tvg-logo`, `group-title`) are parsed and editable. Unknown attributes and extra comment lines are preserved on save.

---

## Project Structure

```
m3u_editor/
├── app.py          # Main application (single file)
└── README.md       # This file
```

---

## License

MIT
