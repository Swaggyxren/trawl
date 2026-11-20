# Trawl — Documentation

Trawl is a GTK4 / libadwaita desktop application for Arch Linux that browses
remote ZIP, RAR and ISO archives over HTTP byte ranges and downloads only the
files the user selects. The archive itself is never stored locally.

## 1. Quick start

```sh
# Launch the GUI
trawl

# Open a link directly from the command line
trawl 'https://example.com/files/archive.zip'
```

In the GUI:

1. Press **+** (or `Ctrl+L`) and paste a direct archive URL.
2. Trawl reads the archive index and lists the files.
3. Tick the files you want, optionally filter by name.
4. Choose a destination folder (default `~/Downloads/<archive name>`).
5. Press **Download**. Per-file progress, cancel and retry are available.

Settings are under `Ctrl+,` and include parallel connections, resume, checksum
verification, timestamp preservation, custom headers, slow mode and insecure
TLS certificates.

## 2. Architecture

```
                ┌──────────────────────┐
   URL / drag ─▶│ ui/window.py         │  single pane, header, session dropdown
                │ ui/add_dialog.py     │  link + headers + options
                │ ui/archive_page.py   │  file list, selection, progress
                └──────────┬───────────┘
                           ▼
                ┌──────────────────────┐
                │ archives/resolvers.py│  host-specific URL candidates
                │ net.py               │  HTTPRangeReader (probe + ranges)
                └──────────┬───────────┘
                           ▼
                ┌──────────────────────┐
                │ archives/{zip,rar,   │  index parser + iter_data()
                │           iso}.py    │
                └──────────┬───────────┘
                           ▼
                ┌──────────────────────┐
                │ downloader.py        │  worker pool, resume, CRC, guard
                └──────────────────────┘
```

Each open URL is a `Session` in `ui/window.py`. Index parsing runs in a daemon
thread; all GTK updates are marshalled with `GLib.idle_add`. Downloads use a
small worker pool (`DownloadJob`) with a separate coordinator thread that emits
the final state.

## 3. Modules

| File | Responsibility |
|---|---|
| `src/trawl/app.py` | `Adw.Application`, startup, CSS/icons, actions, CLI URL handling |
| `src/trawl/net.py` | Thread-safe HTTP range client, redirects, retries, rate limiting, probing, HTML page fetch |
| `src/trawl/archives/base.py` | `Entry`, `Archive`, buffered range helpers, path sanitisation |
| `src/trawl/archives/zip.py` | ZIP/ZIP64 central directory parser and stored/deflate/bzip2/zstd extractor |
| `src/trawl/archives/rar.py` | RAR4/RAR5 header walkers; stored entries only |
| `src/trawl/archives/iso.py` | ISO 9660 directory walk, Joliet and Rock Ridge names |
| `src/trawl/archives/resolvers.py` | Host-specific direct-link resolution and HTML scraping |
| `src/trawl/downloader.py` | Parallel download queue, resume, CRC-32, destination safety |
| `src/trawl/settings.py` | JSON settings and recent-link history (atomic writes, mode 0600) |
| `src/trawl/launchlog.py` | Append-only JSONL launch/error diagnostics with URL redaction |
| `src/trawl/util.py` | Formatting, header parsing, path safety, common-root stripping |
| `src/trawl/style.css` | Neubrutalism skin |
| `src/trawl/ui/` | GTK4/libadwaita windows, dialogs and the archive browser page |

## 4. How remote access works

`HTTPRangeReader` probes with `Range: bytes=0-0`. A valid archive host must
answer `206 Partial Content` with `Content-Range`. A `200` response means the
server ignores ranges and Trawl refuses to continue — it will not silently
download the whole file.

Ranges are sent with `Accept-Encoding: identity` so byte offsets always map to
the stored file. Each worker thread keeps its own keep-alive connections.
Retries resume from the last byte received, not from zero.

Index reads are small and targeted:

- **ZIP**: tail scan for the EOCD record, optional ZIP64 locator, then one
  buffered read of the central directory. File data offsets come from local
  headers, fetched only when a file is downloaded.
- **RAR**: header chain from the signature; each file header yields name, size,
  CRC and data offset.
- **ISO**: volume descriptors at sector 16, then a directory-tree walk using
  2 KiB sectors.

## 5. Format support

| Format | Listing | Extraction |
|---|---|---|
| ZIP / ZIP64 | full central directory | stored, deflate, bzip2, zstd (Python 3.14+) |
| RAR 4 / RAR 5 | full header chain | stored entries only |
| ISO 9660 | Joliet and Rock Ridge | all files |

Not supported: compressed RAR, encrypted archives, encrypted RAR headers, 7z,
tar/gzip/bzip2/xz streams, UDF-only ISO images, split/multi-volume archives,
Google Drive and Mega links.

## 6. Link resolvers

`resolve_candidates()` turns a pasted URL into ordered direct-download
candidates:

- Pixeldrain `/u/<id>` → `/api/file/<id>?download`
- Dropbox `/s/...` → adds `dl=1`
- Buzzheavier → tries the `/download` suffix
- MediaFire → lands on the page, then scrapes it
- Generic hosts → used as-is; if the response is HTML, `scrape_archive_url()`
  looks for archive links or download-style URLs in the page

At most two landing-page hops are followed to avoid loops.

## 7. Security model

- Entry paths are normalised: backslashes become `/`, leading `./` and `/` are
  removed; `..`, drive letters, NUL bytes and over-long components are rejected.
- `DestinationGuard` creates directories with `os.mkdir` and checks each
  component with `lstat`; symlinks are never followed or created.
- Pasted links are validated before any session exists: whitespace, over-long
  input, non-http(s) schemes and hosts without a dot/IP/localhost are rejected.
  Bare `host/path` input is upgraded to https only when it has no whitespace.
- User-facing errors are mapped to plain messages; Python exception text is
  never displayed.
- Custom headers cannot override `Host`, `Range`, `Content-Length`,
  `Connection`, `Accept-Encoding`, `Transfer-Encoding` or `If-Range`.
- CRC-32 is checked when the archive provides one; corrupt downloads are deleted.
- Settings are written atomically with `0o600` permissions.

## 8. Launch diagnostics

`LaunchLog` writes one JSON object per line to
`$XDG_DATA_HOME/trawl/launches.jsonl` (default `~/.local/share/trawl/`). A
`start` event is written immediately, followed by redacted `url`, `archive`,
`download` and `error` events, and an `end` event with duration and counts when
the app shuts down or the window closes. All events in a launch share a UUID
session id. Credentials, query strings, fragments and custom headers are
stripped; the log file is created with mode `0600` and write failures are
ignored so diagnostics can never break the app.

## 9. UI design

The interface uses an expanded Neubrutalism skin:

- paper `#FFF8E7` on a subtle graph-paper canvas, ink `#000000`
- fills: yellow `#FFE14D`, pink `#FF7AB6`, blue `#4D9DFF`, lime `#B6F23C`,
  lilac `#C9A8FF`, orange `#FF9A3C` and red `#FF5A5A`
- 3–5 px black outlines, zero-radius corners, hard zero-blur offset shadows
- square window controls with a red close button, sticker-style group headings

CSS classes are mapped onto existing widgets rather than replacing layout:

| Class | Applied to |
|---|---|
| `nb-root` | main window, archive page, dialogs |
| `nb-canvas` | graph-paper background behind the empty-state hero |
| `nb-note` | sticky-note hint under the empty-state hero |
| `nb-hero`, `nb-icon-tile`, `nb-hero-title`, `nb-hero-body` | empty-state card |
| `nb-step`, `nb-step-num`, `nb-chip`, `nb-cta` | onboarding steps, format chips, primary call to action |
| `nb-card`, `nb-card-white`, `nb-panel` | status/notice blocks and preference groups |
| `nb-tag`, `nb-tag-blue` | selection count, format badges |
| `nb-button`, `nb-secondary`, `nb-red`, `nb-orange` | action buttons |
| `nb-input` | URL search, header editors |
| `nb-file-row`, `nb-session-row` | file list and session/recent rows |
| `nb-icon-button` | icon-only controls |
| `nb-choice-card`, `nb-choice-all`, `nb-choice-select` | two-card archive choice screen |
| `nb-progress`, `nb-alert` | download progress and modal alert windows |

After indexing, `ArchivePage` shows a two-card choice screen unless the archive
has one file or the `choice_skip` setting is enabled. **Download all** opens a
confirmation with the destination; **Select files** opens the existing browser.
`last_choice` is remembered but the screen is always shown on open unless
skipped. Keyboard `1`/`2` choose the cards and `Esc` returns from either the
browser or the confirmation to the choice screen.

Confirmation popups use `ui/alert.py` (`TrawlAlert`), a modal `Adw.Window`
rather than `Adw.AlertDialog`: libadwaita paints the AlertDialog sheet and its
internal gizmos white on a dark, rounded surface that CSS cannot reliably
override, while a modal window is fully covered by our stylesheet.

Contrast: black text is used on every bright fill and white only on black;
focus rings are 4 px black outlines with offset, including switches. Every
icon-only control has an accessible name. The stylesheet requests Space
Grotesk with a system fallback (the font is not bundled). Motion is disabled
when the desktop requests reduced animations.

## 10. Development

```sh
python -m py_compile $(find src -name '*.py')
PYTHONPATH=src python -m unittest discover -s tests -v
python -m build --wheel --no-isolation
```

The test suite is hermetic: `tests/fixtures.py` generates ZIP, RAR4, RAR5 and
ISO images with the standard library, and `tests/support.py` runs a local
byte-range HTTP server. No external tools or network access are needed.

## 11. Arch packaging

`PKGBUILD` builds a wheel with `python -m build` and installs it with
`python -m installer`, plus the desktop entry, AppStream metainfo, icons and
license. Create a source tarball first:

```sh
git archive --format=tar.gz --prefix=trawl-0.1.0/ HEAD > trawl-0.1.0.tar.gz
makepkg -si
```

Runtime dependencies: `python`, `python-gobject`, `gtk4`, `libadwaita`.
`python-compression` is optional and enables zstd ZIP entries on Python 3.14+.

## 12. Credits

Inspired by [ZipStream](https://github.com/Yexclsv25/ZipStream) (MIT) by
Yexclsv25. Trawl is an independent desktop reimagining, not a port — the
original repository contains no source code. Trawl is MIT licensed.
