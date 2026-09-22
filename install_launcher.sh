#!/usr/bin/env bash
set -e

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
DESKTOP_DIR="$HOME/.local/share/applications"
DESKTOP_FILE="$DESKTOP_DIR/medianotes.desktop"

mkdir -p "$DESKTOP_DIR"

rm -f "$DESKTOP_DIR/media-notes.desktop"

cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=MediaNotes
Comment=Obrázky, GIFy, videa a poznámky
Exec=/bin/bash $APP_DIR/start_app.sh
Path=$APP_DIR
Icon=image-x-generic
Terminal=false
Categories=Graphics;Utility;
StartupNotify=true
EOF

chmod +x "$APP_DIR/start_app.sh" || true

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
fi

echo "Spouštěč MediaNotes byl nainstalován:"
echo "$DESKTOP_FILE"
echo "Exec=/bin/bash $APP_DIR/start_app.sh"
