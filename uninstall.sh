#!/usr/bin/env bash
set -euo pipefail

SHARE_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/backup"
BIN="$HOME/.local/bin/backup"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PURGE="${1:-}"

if [ "$PURGE" = "--purge" ]; then
  echo "Stopping and removing all backup timers..."
  for timer in "$UNIT_DIR"/backup-*.timer; do
    [ -e "$timer" ] || continue
    unit="$(basename "$timer")"
    systemctl --user disable --now "$unit" 2>/dev/null || true
  done
  rm -f "$UNIT_DIR"/backup-*.timer "$UNIT_DIR"/backup-*.service
  systemctl --user daemon-reload 2>/dev/null || true
  rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/backup" \
         "${XDG_STATE_HOME:-$HOME/.local/state}/backup"
  echo "Purged jobs, timers, config, and state (snapshots on destinations kept)."
fi

rm -f "$BIN"
rm -rf "$SHARE_DIR"
echo "Removed backup CLI."
