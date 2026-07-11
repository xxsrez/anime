#!/usr/bin/env python3
import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import server
from scripts.atomic_publish import atomic_publish_directory
from scripts.db_data_diff import quote_identifier, sql_literal
from scripts.operation_lock import DatabaseOperationLock, default_lock_path


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "db" / "animego.sqlite"
DEFAULT_OUT = ROOT / "db" / "backups" / "current"
USER_STATE_TABLES = (
    "user_title_state",
    "user_title_navigation_state",
    "user_episode_state",
    "user_watch_events",
)


def connect(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("pragma foreign_keys=on")
    con.execute("pragma busy_timeout=30000")
    return con


def scalar(con, sql, params=()):
    return con.execute(sql, params).fetchone()[0]


def backup_database(db_path, out_dir):
    backup_path = out_dir / "animego.sqlite"
    source = sqlite3.connect(db_path)
    temp_path = out_dir / ".animego.sqlite.tmp"
    temp_path.unlink(missing_ok=True)
    target = sqlite3.connect(temp_path)
    try:
        with target:
            source.backup(target)
    finally:
        target.close()
        source.close()
    os.replace(temp_path, backup_path)
    return backup_path


def user_state_rows(con):
    return [
        dict(row)
        for row in con.execute(
            """
            select
                us.user_id,
                coalesce(u.email, u.name, u.google_sub) as user_label,
                us.anime_id,
                a.title,
                a.source,
                a.source_id,
                us.is_favorite,
                us.progress_episode_number,
                us.watched,
                us.updated_at
            from user_title_state us
            left join users u on u.id = us.user_id
            left join anime a on a.id = us.anime_id
            order by coalesce(u.email, u.name, u.google_sub, ''), coalesce(a.title, ''), us.anime_id
            """
        )
    ]


def write_user_state_sql(con, out_dir):
    table_specs = []
    for table in USER_STATE_TABLES:
        columns = [row[1] for row in con.execute(f'pragma table_info("{table}")')]
        if not columns:
            raise RuntimeError(f"missing user-state table: {table}")
        pk_columns = [
            row[1]
            for row in sorted(
                (row for row in con.execute(f'pragma table_info("{table}")') if row[5]),
                key=lambda row: row[5],
            )
        ]
        order_sql = ", ".join(quote_identifier(column) for column in pk_columns) or "rowid"
        table_specs.append((table, columns, order_sql))

    path = out_dir / "user_state.sql"
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("PRAGMA foreign_keys=ON;\nBEGIN IMMEDIATE;\n")
        for table in reversed(USER_STATE_TABLES):
            handle.write(f"DELETE FROM {quote_identifier(table)};\n")
        for table, columns, order_sql in table_specs:
            column_sql = ", ".join(quote_identifier(column) for column in columns)
            for row in con.execute(f'select * from "{table}" order by {order_sql}'):
                values = ", ".join(sql_literal(value) for value in tuple(row))
                handle.write(
                    f"INSERT INTO {quote_identifier(table)} ({column_sql}) VALUES ({values});\n"
                )
        handle.write("COMMIT;\n")
    return path


def write_user_state_json(rows, out_dir):
    path = out_dir / "user_title_state.json"
    path.write_text(json.dumps(rows, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    return path


def write_user_state_csv(rows, out_dir):
    path = out_dir / "user_title_state.csv"
    fields = [
        "user_id",
        "user_label",
        "anime_id",
        "title",
        "source",
        "source_id",
        "is_favorite",
        "progress_episode_number",
        "watched",
        "updated_at",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return path


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_checksums(paths, out_dir):
    path = out_dir / "SHA256SUMS"
    lines = []
    for item in paths:
        rel = item.relative_to(out_dir)
        lines.append(f"{sha256_file(item)}  {rel.as_posix()}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def collect_counts(con, db_path):
    items = server.get_anime_list(str(db_path))
    source_counts = {
        row["source"] or "unknown": row["count"]
        for row in con.execute(
            "select coalesce(source, 'unknown') source, count(*) count from anime group by coalesce(source, 'unknown')"
        )
    }
    return {
        "source_title_rows": scalar(con, "select count(*) from anime"),
        "canonical_catalog_titles": len(items),
        "merged_pairs": sum(1 for item in items if (item.get("source_variant_count") or 1) > 1),
        "animego_source_titles": source_counts.get("animego", 0),
        "yummyanime_source_titles": source_counts.get("yummyanime", 0),
        "episodes": scalar(con, "select count(*) from episodes"),
        "playable_video_sources": scalar(con, "select count(*) from video_sources where embed_url is not null"),
        "non_playable_source_rows": scalar(
            con,
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
        ),
        "user_state_rows": scalar(con, "select count(*) from user_title_state"),
        "user_title_navigation_rows": scalar(con, "select count(*) from user_title_navigation_state"),
        "user_episode_state_rows": scalar(con, "select count(*) from user_episode_state"),
        "user_watch_event_rows": scalar(con, "select count(*) from user_watch_events"),
        "favorites": scalar(con, "select count(*) from user_title_state where is_favorite = 1"),
        "titles_with_progress": scalar(con, "select count(*) from user_title_state where progress_episode_number is not null"),
        "watched_titles": scalar(con, "select count(*) from user_title_state where watched = 1"),
    }


def validate_snapshot(db_path):
    con = connect(db_path)
    try:
        integrity = con.execute("pragma integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"integrity_check failed: {integrity}")
        fk_errors = con.execute("pragma foreign_key_check").fetchall()
        if fk_errors:
            raise RuntimeError(
                f"foreign_key_check failed: {[tuple(row) for row in fk_errors[:5]]}"
            )
        non_playable = scalar(
            con,
            """
            select count(*)
            from anime a
            where not exists (
                select 1 from video_sources vs
                where vs.anime_id = a.id and vs.embed_url is not null
            )
            """,
        )
        if non_playable:
            raise RuntimeError(f"non-playable source rows: {non_playable}")
    finally:
        con.close()


def write_readme(out_dir, created_at, counts):
    path = out_dir / "README.md"
    body = f"""# Current Backup

Created: {created_at}

This directory is the local recovery snapshot for the current catalog state.
It lives under `db/`, which is intentionally ignored by git.

## Contents

- `animego.sqlite` - full SQLite backup made with SQLite's backup API.
- `user_state.sql` - transactional SQL dump of title, episode, and watch-event state.
- `user_title_state.json` - readable user-state export with title/source data.
- `user_title_state.csv` - spreadsheet-friendly user-state export.
- `SHA256SUMS` - checksums for the staged backup and export files.

## Snapshot Counts

- Source title rows: {counts['source_title_rows']}.
- Canonical catalog titles: {counts['canonical_catalog_titles']}.
- Merged AnimeGO/YummyAnime pairs: {counts['merged_pairs']}.
- AnimeGO source titles: {counts['animego_source_titles']}.
- YummyAnime/YummyAni source titles: {counts['yummyanime_source_titles']}.
- Episodes: {counts['episodes']}.
- Playable video sources: {counts['playable_video_sources']}.
- Non-playable source rows: {counts['non_playable_source_rows']}.
- User-state rows: {counts['user_state_rows']}.
- Title-navigation rows: {counts['user_title_navigation_rows']}.
- Episode-state rows: {counts['user_episode_state_rows']}.
- Watch-event rows: {counts['user_watch_event_rows']}.
- Favorites: {counts['favorites']}.
- Titles with progress: {counts['titles_with_progress']}.
- Watched titles: {counts['watched_titles']}.

## Restore Full Database

From the project root:

```bash
cp db/backups/current/animego.sqlite db/animego.sqlite
sqlite3 db/animego.sqlite 'pragma integrity_check;'
```

## Restore Only User State

From the project root:

```bash
sqlite3 -bail db/animego.sqlite < db/backups/current/user_state.sql
```

The full backup and active database can have different SHA-256 hashes because
SQLite backup can rewrite page layout while preserving the logical database.
"""
    path.write_text(body, encoding="utf-8")
    return path


def update_backup(args):
    raw_db_path = Path(args.db).expanduser().absolute()
    db_path = raw_db_path.resolve()
    raw_out_dir = Path(args.out).expanduser().absolute()
    if raw_out_dir.is_symlink():
        raise ValueError(f"backup output must not be a symlink: {raw_out_dir}")
    out_dir = raw_out_dir.resolve()
    if (
        out_dir == db_path
        or out_dir in db_path.parents
        or raw_out_dir == raw_db_path
        or raw_out_dir in raw_db_path.parents
    ):
        raise ValueError(f"backup output must not be the database or contain it: {out_dir}")
    if out_dir.exists() and not out_dir.is_dir():
        raise ValueError(f"backup output must be a directory: {out_dir}")
    if not db_path.is_file() or db_path.stat().st_size == 0:
        raise FileNotFoundError(f"database does not exist or is empty: {db_path}")
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    lock_path = getattr(args, "lock_file", None) or default_lock_path(db_path)

    with atomic_publish_directory(
        out_dir,
        stage_prefix=f".{out_dir.name}.stage-",
        previous_prefix=f".{out_dir.name}.previous-",
        wait=getattr(args, "wait_lock", False),
        timeout=getattr(args, "lock_timeout", 30.0),
        operation="backup publication",
    ) as stage:
        with DatabaseOperationLock(
            db_path,
            path=lock_path,
            wait=getattr(args, "wait_lock", False),
            timeout=getattr(args, "lock_timeout", 30.0),
            operation="backup snapshot",
        ):
            backup_path = backup_database(db_path, stage)

        con = connect(backup_path)
        try:
            counts = collect_counts(con, backup_path)
            rows = user_state_rows(con)
            sql_path = write_user_state_sql(con, stage)
        finally:
            con.close()

        json_path = write_user_state_json(rows, stage)
        csv_path = write_user_state_csv(rows, stage)
        created_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        readme_path = write_readme(stage, created_at, counts)

        # All operations above use the isolated staged copy. Validate its final
        # state exactly once, immediately before checksumming and publication.
        validate_snapshot(backup_path)
        checksum_path = write_checksums(
            [backup_path, sql_path, json_path, csv_path, readme_path], stage
        )

    backup_path = out_dir / backup_path.name
    sql_path = out_dir / sql_path.name
    json_path = out_dir / json_path.name
    csv_path = out_dir / csv_path.name
    readme_path = out_dir / readme_path.name
    checksum_path = out_dir / checksum_path.name
    print(f"wrote {backup_path}")
    print(f"wrote {sql_path}")
    print(f"wrote {json_path}")
    print(f"wrote {csv_path}")
    print(f"wrote {readme_path}")
    print(f"wrote {checksum_path}")


def main():
    parser = argparse.ArgumentParser(description="Refresh db/backups/current from the active SQLite database.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--lock-file")
    parser.add_argument("--wait-lock", action="store_true")
    parser.add_argument("--lock-timeout", type=float, default=30.0)
    update_backup(parser.parse_args())


if __name__ == "__main__":
    main()
