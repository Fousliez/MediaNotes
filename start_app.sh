#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

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
