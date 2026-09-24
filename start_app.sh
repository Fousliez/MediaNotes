#!/usr/bin/env bash
set -e

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

# Jednorázově přejmenuj existující položku v nabídce aplikací.
LEGACY_DESKTOP="$HOME/.local/share/applications/medianotes.desktop"
if [ -f "$LEGACY_DESKTOP" ]; then
    sed -i 's/^Name=MediaNotes$/Name=Zobrazovač/' "$LEGACY_DESKTOP" 2>/dev/null || true
fi

# Lokální pracovní kopie je určená pro běh aplikace.
# Při spuštění ji srovnáme s aktuální větví origin/main.
if [ -d ".git" ]; then
    git config core.fileMode false >/dev/null 2>&1 || true

    if timeout 12s git fetch --quiet origin main >/dev/null 2>&1; then
        git reset --hard --quiet origin/main >/dev/null 2>&1 || true
    fi
fi

if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi

source .venv/bin/activate

REQ_HASH="$(sha256sum requirements.txt | awk '{print $1}')"
STAMP_FILE=".venv/.requirements.sha256"
OLD_HASH=""

if [ -f "$STAMP_FILE" ]; then
    OLD_HASH="$(cat "$STAMP_FILE")"
fi

if [ "$REQ_HASH" != "$OLD_HASH" ]; then
    python -m pip install -q -r requirements.txt
    printf '%s' "$REQ_HASH" > "$STAMP_FILE"
fi

exec python main.py
