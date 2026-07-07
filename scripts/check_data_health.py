#!/usr/bin/env python3
import argparse
import hashlib
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def integrity_check(path):
    con = sqlite3.connect(path)
    try:
        result = con.execute("pragma integrity_check").fetchone()[0]
    finally:
        con.close()
    if result != "ok":
        raise AssertionError(f"{path}: integrity_check returned {result!r}")


def scalar(path, sql):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql).fetchone()[0]
    finally:
        con.close()


def verify_checksums(path):
    if not path.exists():
        raise AssertionError(f"missing checksum file: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        expected, rel = line.split(None, 1)
        item = ROOT / rel.strip()
        if not item.exists():
            raise AssertionError(f"checksum target missing: {rel}")
        actual = sha256_file(item)
        if actual != expected:
            raise AssertionError(f"checksum mismatch: {rel}")


def check(args):
    db_path = Path(args.db)
    backup_dir = Path(args.backup_dir)
    backup_db = backup_dir / "animego.sqlite"
    checksum_path = backup_dir / "SHA256SUMS"

    for path in (db_path, backup_db):
        if not path.exists():
            raise AssertionError(f"missing database: {path}")
        integrity_check(path)

    non_playable = scalar(
        db_path,
        """
        select count(*)
        from anime a
        where not exists (
            select 1
            from video_sources vs
            where vs.anime_id = a.id
              and vs.embed_url is not null
        )
        """,
    )
    if non_playable:
        raise AssertionError(f"non-playable source rows: {non_playable}")

    verify_checksums(checksum_path)

    counts = {
        "anime": scalar(db_path, "select count(*) from anime"),
        "episodes": scalar(db_path, "select count(*) from episodes"),
        "video_sources": scalar(db_path, "select count(*) from video_sources where embed_url is not null"),
        "user_title_state": scalar(db_path, "select count(*) from user_title_state"),
    }
    print("data health ok: " + ", ".join(f"{key}={value}" for key, value in counts.items()))


def main():
    parser = argparse.ArgumentParser(description="Validate the local ignored SQLite catalog and backup snapshot.")
    parser.add_argument("--db", default=str(ROOT / "db" / "animego.sqlite"))
    parser.add_argument("--backup-dir", default=str(ROOT / "db" / "backups" / "current"))
    args = parser.parse_args()
    try:
        check(args)
    except AssertionError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
