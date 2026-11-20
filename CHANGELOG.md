# Changelog

## v0.1.0

### Added

- Remote ZIP, ZIP64, stored RAR 4/5 and ISO 9660 browsing over HTTP byte ranges.
- Two-choice screen after indexing: Download all or Select files.
- Parallel downloads with resume, checksum verification and failure reporting.
- Download-finished dialog, desktop notification and freedesktop sound.
- Install/uninstall scripts for Arch, Debian/Ubuntu, Fedora and openSUSE.
- Diagnostics log with redacted URLs and short error codes.

### Fixed

- Link validation before a session or log entry is created.
- Structured logging without pasted free text.
- Square window controls without libadwaita's grey circles.
- Responsive home and choice screens at small window sizes.

### Changed

- App identity moved to `io.github.Swaggyxren.Trawl`.
- Home screen reorganised into one centered column.
- Logo redrawn to match the Neubrutalism style.
