#!/usr/bin/env python3
import argparse
import http.client
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server


def request(host, port, method, path, headers=None, body=None):
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    payload = response.read()
    return response.status, dict(response.getheaders()), payload


def assert_status(actual, expected, label):
    if actual != expected:
        raise AssertionError(f"{label}: expected HTTP {expected}, got {actual}")


def make_smoke_db(source_db, tmpdir):
    smoke_db = Path(tmpdir) / "animego-smoke.sqlite"
    if source_db.exists():
        shutil.copy2(source_db, smoke_db)
    server.prepare_database(smoke_db)
    return smoke_db


def create_smoke_session(db_path):
    con = server.connect(db_path)
    try:
        title_count = con.execute("select count(*) from anime").fetchone()[0]
        if not title_count:
            return None
        user = server.upsert_google_user(
            con,
            {
                "sub": "local-smoke-user",
                "email": "smoke@example.test",
                "email_verified": True,
                "name": "Smoke Test",
                "picture": None,
            },
        )
        token, _ = server.create_session(con, user["id"])
        con.commit()
        return token
    finally:
        con.close()


def smoke(args):
    source_db = Path(args.db)
    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ.setdefault("ANIME_LOG_DIR", str(Path(tmpdir) / "logs"))
        smoke_db = make_smoke_db(source_db, tmpdir)
        token = create_smoke_session(smoke_db)

        httpd = server.ThreadingHTTPServer((args.host, 0), server.AnimeHandler)
        httpd.db_path = str(smoke_db)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_port

            status, _, body = request(args.host, port, "GET", "/api/health")
            assert_status(status, 200, "health")
            if json.loads(body) != {"ok": True}:
                raise AssertionError("health returned unexpected payload")

            status, _, _ = request(args.host, port, "GET", "/api/auth/config")
            assert_status(status, 200, "auth config")

            status, headers, _ = request(args.host, port, "GET", "/")
            assert_status(status, 302, "unauthenticated index")
            if not headers.get("Location", "").startswith("/login?next="):
                raise AssertionError("unauthenticated index did not redirect to login")

            status, _, body = request(args.host, port, "GET", "/login")
            assert_status(status, 200, "login page")
            if b"Anime Catalog" not in body:
                raise AssertionError("login page did not render app brand")

            status, _, _ = request(args.host, port, "GET", "/api/anime")
            assert_status(status, 401, "unauthenticated catalog API")

            if token:
                cookie = {"Cookie": f"{server.SESSION_COOKIE_NAME}={token}"}
                status, _, body = request(args.host, port, "GET", "/", headers=cookie)
                assert_status(status, 200, "authenticated index")
                if b"anime-list" not in body:
                    raise AssertionError("authenticated index did not render app shell")

                status, _, body = request(args.host, port, "GET", "/api/anime", headers=cookie)
                assert_status(status, 200, "authenticated catalog API")
                items = json.loads(body).get("items") or []
                if not items:
                    raise AssertionError("catalog API returned no items for local snapshot")

                status, _, body = request(args.host, port, "GET", "/api/recommendations?limit=20", headers=cookie)
                assert_status(status, 200, "recommendations API")
                if "items" not in json.loads(body):
                    raise AssertionError("recommendations API returned unexpected payload")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    print("smoke ok")


def main():
    parser = argparse.ArgumentParser(description="Smoke-test the local Anime app HTTP surface.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--db", default=str(ROOT / "db" / "animego.sqlite"))
    args = parser.parse_args()
    smoke(args)


if __name__ == "__main__":
    main()
