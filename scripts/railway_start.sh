#!/usr/bin/env sh
set -eu

volume_dir="${RAILWAY_VOLUME_MOUNT_PATH:-/data}"
db_path="${ANIMEGO_DB:-${volume_dir}/animego.sqlite}"
log_dir="${ANIME_LOG_DIR:-${volume_dir}/logs}"

mkdir -p "$(dirname "$db_path")" "$log_dir"
export ANIME_LOG_DIR="$log_dir"

if [ ! -s "$db_path" ]; then
  echo "Missing SQLite database at $db_path." >&2
  echo "Upload the production animego.sqlite into the Railway volume before starting the app." >&2
  exit 1
fi

exec python3 server.py --host 0.0.0.0 --port "${PORT:-8765}" --db "$db_path"
