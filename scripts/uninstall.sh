#!/bin/bash
# uninstall.sh — remove the Faustus service/runtime on Linux/macOS.
#
# Default (no flags): stops/disables the systemd service if installed and
# leaves data/, .env and backups/ untouched — an uninstall must never
# destroy a user's chats, memories and backups by accident.
#
# --purge: ALSO deletes data/, .env and backups/ under this checkout. Only
# runs after an explicit --purge on the command line; there is no other
# path to that deletion in this script.
#
# See docs/spec/distribution/LIFECYCLE.md for the full clean-install / upgrade /
# backup / restore / uninstall lifecycle this script is one step of.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PURGE=0
YES=0

for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    --keep-data) PURGE=0 ;;
    --yes) YES=1 ;;
    -h|--help)
      echo "Usage: $0 [--purge|--keep-data] [--yes]"
      echo "  --keep-data   (default) preserve data/, .env, backups/"
      echo "  --purge       also delete data/, .env, backups/ under $SCRIPT_DIR"
      exit 0
      ;;
  esac
done

echo "Uninstalling Faustus service/runtime from $SCRIPT_DIR ..."

if command -v systemctl >/dev/null 2>&1; then
  if systemctl list-unit-files 2>/dev/null | grep -q '^faustus-ui\.service'; then
    echo "[+] Stopping and disabling faustus-ui.service"
    sudo systemctl stop faustus-ui 2>/dev/null || true
    sudo systemctl disable faustus-ui 2>/dev/null || true
    sudo rm -f /etc/systemd/system/faustus-ui.service
    sudo systemctl daemon-reload 2>/dev/null || true
  else
    echo "[i] No faustus-ui.service installed; skipping service removal."
  fi
else
  echo "[i] systemctl not found; skipping service removal (not on this platform)."
fi

if [ "$PURGE" -eq 1 ]; then
  if [ "$YES" -ne 1 ]; then
    read -r -p "This will PERMANENTLY delete data/, .env and backups/ under $SCRIPT_DIR. Type 'yes' to continue: " confirm
    if [ "$confirm" != "yes" ]; then
      echo "Aborted; nothing under data/ was deleted."
      exit 1
    fi
  fi
  echo "[+] --purge: deleting data/, .env, backups/"
  rm -rf "$SCRIPT_DIR/data" "$SCRIPT_DIR/.env" "$SCRIPT_DIR/backups"
else
  echo "[i] Keeping data/, .env and backups/ (pass --purge to delete them)."
fi

echo "Done."
