#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
LOG_DIR=${HOME}/Library/Logs/Anime

mkdir -p "$LOG_DIR"
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin

exec /usr/bin/caffeinate -i "$ROOT/.venv/bin/python" "$ROOT/scripts/animego_push_worker.py" \
  >>"$LOG_DIR/animego-push-worker.log" 2>&1
