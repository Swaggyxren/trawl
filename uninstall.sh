#!/usr/bin/env bash
# Remove Trawl. Downloaded files, settings and logs are kept unless --purge is passed.
set -euo pipefail

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

PURGE=0
case "${1:-}" in
  --purge) PURGE=1 ;;
  ""|-h|--help)
    printf 'Usage: ./uninstall.sh [--purge]\n'
    printf '  --purge  also remove local settings and diagnostic logs\n'
    [[ "${1:-}" == "" ]] || exit 0
    ;;
  *) die "Unknown option: $1" ;;
esac

if [[ "${EUID}" -eq 0 ]]; then
  die "Do not run this script as root; it only removes user-local files."
fi

say "Removing pipx package"
pipx uninstall trawl 2>/dev/null || python3 -m pipx uninstall trawl 2>/dev/null || true

say "Removing desktop entry and icons"
rm -f "${HOME}/.local/share/applications/io.github.Swaggyxren.Trawl.desktop"
rm -f "${HOME}/.local/share/icons/hicolor/scalable/apps/io.github.Swaggyxren.Trawl.svg"
rm -f "${HOME}/.local/share/icons/hicolor/256x256/apps/io.github.Swaggyxren.Trawl.png"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "${HOME}/.local/share/applications" >/dev/null 2>&1 || true
fi
if command -v gtk4-update-icon-cache >/dev/null 2>&1; then
  gtk4-update-icon-cache -q -f "${HOME}/.local/share/icons/hicolor" >/dev/null 2>&1 || true
elif command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q -f "${HOME}/.local/share/icons/hicolor" >/dev/null 2>&1 || true
fi

if [[ "${PURGE}" -eq 1 ]]; then
  say "Purging settings and diagnostic logs"
  rm -rf "${HOME}/.config/trawl" "${HOME}/.local/share/trawl"
else
  printf '\nDownloaded files, settings and logs were left untouched.\n'
  printf 'Use --purge to remove settings and logs as well.\n'
fi

say "Trawl uninstalled."
