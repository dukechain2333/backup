#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARE_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/backup"
BIN_DIR="$HOME/.local/bin"
BIN="$BIN_DIR/backup"

echo "Installing backup CLI..."
mkdir -p "$SHARE_DIR" "$BIN_DIR"
rm -rf "$SHARE_DIR/backup"
cp -r "$REPO_DIR/src/backup" "$SHARE_DIR/backup"

cat > "$BIN" <<EOF
#!/usr/bin/env bash
exec env BACKUP_SHARE_DIR="$SHARE_DIR" python3 -c 'import os, sys; sys.path.insert(0, os.environ["BACKUP_SHARE_DIR"]); from backup.cli import main; sys.exit(main())' "\$@"
EOF
chmod +x "$BIN"

# Ensure ~/.local/bin is on PATH
if ! echo ":$PATH:" | grep -qF ":$BIN_DIR:"; then
  case "${SHELL:-}" in
    */zsh) RC="$HOME/.zshrc" ;;
    *)     RC="$HOME/.bashrc" ;;
  esac
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$RC"
  echo "Added ~/.local/bin to PATH in $RC (open a new shell or 'source $RC')."
fi

# Create config/state dirs
"$BIN" list >/dev/null 2>&1 || true

# Enable linger so user timers run when logged out
if command -v loginctl >/dev/null 2>&1; then
  if loginctl enable-linger "${USER:-$(id -un)}" 2>/dev/null; then
    echo "Enabled linger for ${USER:-$(id -un)} (timers run when logged out)."
  else
    echo "Note: could not enable linger; timers run only while you are logged in."
  fi
fi

echo "Done. Try:  backup add --dest /path/to/backups --schedule daily@02:00"
