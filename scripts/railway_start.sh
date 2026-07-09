#!/usr/bin/env sh
set -eu

if [ "${ANIME_SERVICE_ROLE:-web}" = "daily-sync" ]; then
  exec python3 scripts/railway_daily_sync.py
fi

volume_dir="${RAILWAY_VOLUME_MOUNT_PATH:-/data}"
db_path="${ANIMEGO_DB:-${volume_dir}/animego.sqlite}"
log_dir="${ANIME_LOG_DIR:-${volume_dir}/logs}"

mkdir -p "$(dirname "$db_path")" "$log_dir"
export ANIME_LOG_DIR="$log_dir"

if [ ! -s "$db_path" ]; then
  echo "Missing SQLite database at $db_path." >&2
  echo "Starting a temporary health server so the Railway volume can be populated." >&2
fi
export ANIME_MISSING_DB_PATH="$db_path"
exec python3 scripts/missing_db_bootstrap.py
