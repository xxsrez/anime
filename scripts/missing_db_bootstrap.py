#!/usr/bin/env python3
"""503 readiness server that hands off automatically after a DB upload."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_TABLES = {"anime", "episodes", "video_sources"}


def database_ready(db_path):
    path = Path(db_path)
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        uri = path.resolve().as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=1)
        try:
            con.execute("pragma query_only=on")
            if con.execute("pragma quick_check(1)").fetchone()[0] != "ok":
                return False
            tables = {
                row[0]
                for row in con.execute(
                    "select name from sqlite_master where type = 'table'"
                )
            }
            return REQUIRED_TABLES.issubset(tables)
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return False


def database_fingerprint(db_path):
    try:
        stat = Path(db_path).stat()
        return stat.st_size, stat.st_mtime_ns
    except OSError:
        return None


class MissingDatabaseHandler(BaseHTTPRequestHandler):
    db_path = ""

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def do_GET(self):
        body = json.dumps(
            {
                "ok": False,
                "ready": False,
                "error": "database missing or not ready",
                "db_path": self.db_path,
            }
        ).encode("utf-8")
        self.send_response(503)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def wait_for_database(httpd, db_path, interval=1.0):
    # Do not hand the file to the application while an upload is still
    # replacing it. Require the same valid size/mtime on two consecutive
    # probes; this also covers upload implementations that write in place.
    stable_fingerprint = None
    while True:
        fingerprint = database_fingerprint(db_path) if database_ready(db_path) else None
        if fingerprint is not None and fingerprint == stable_fingerprint:
            break
        stable_fingerprint = fingerprint
        threading.Event().wait(interval)
    print(f"SQLite database is ready at {db_path}; starting the application", flush=True)
    httpd.shutdown()


def exec_application(db_path, port):
    argv = [
        sys.executable,
        str(ROOT / "server.py"),
        "--host",
        "0.0.0.0",
        "--port",
        str(port),
        "--db",
        str(db_path),
    ]
    os.execv(sys.executable, argv)


def main():
    db_path = Path(os.environ.get("ANIME_MISSING_DB_PATH") or os.environ.get("ANIMEGO_DB") or "/data/animego.sqlite")
    port = int(os.environ.get("PORT") or "8765")
    if database_ready(db_path):
        exec_application(db_path, port)
        return 0
    MissingDatabaseHandler.db_path = str(db_path)
    httpd = ThreadingHTTPServer(("0.0.0.0", port), MissingDatabaseHandler)
    watcher = threading.Thread(
        target=wait_for_database,
        args=(httpd, db_path),
        name="database-readiness-watcher",
        daemon=True,
    )
    watcher.start()
    print(f"Waiting for a valid SQLite database on 0.0.0.0:{port}", flush=True)
    try:
        httpd.serve_forever(poll_interval=0.25)
    finally:
        httpd.server_close()
    if database_ready(db_path):
        exec_application(db_path, port)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
