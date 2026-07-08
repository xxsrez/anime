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
  export ANIME_MISSING_DB_PATH="$db_path"
  exec python3 - <<'PY'
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MissingDatabaseHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))

    def send_payload(self, payload, status):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        payload = {
            "ok": self.path == "/api/health",
            "error": "database missing",
            "db_path": os.environ.get("ANIME_MISSING_DB_PATH"),
        }
        self.send_payload(payload, 200 if self.path == "/api/health" else 503)


port = int(os.environ.get("PORT") or "8765")
server = ThreadingHTTPServer(("0.0.0.0", port), MissingDatabaseHandler)
print(f"Waiting for SQLite database on 0.0.0.0:{port}")
server.serve_forever()
PY
fi

exec python3 server.py --host 0.0.0.0 --port "${PORT:-8765}" --db "$db_path"
