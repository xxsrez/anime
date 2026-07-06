#!/usr/bin/env python3
import argparse
import csv
import datetime as dt
import hashlib
import json
import sqlite3
from pathlib import Path

import server


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "animego.sqlite"
DEFAULT_OUT = ROOT / "backups" / "current"


def connect(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


def scalar(con, sql, params=()):
    return con.execute(sql, params).fetchone()[0]


def backup_database(db_path, out_dir):
    backup_path = out_dir / "animego.sqlite"
    backup_path.unlink(missing_ok=True)
    source = sqlite3.connect(db_path)
    target = sqlite3.connect(backup_path)
    with target:
        source.backup(target)
    target.close()
    source.close()
    return backup_path


def user_state_rows(con):
    return [
        dict(row)
        for row in con.execute(
            """
            select
                us.anime_id,
                a.title,
                a.source,
                a.source_id,
                us.is_favorite,
                us.progress_episode_number,
                us.watched,
                us.updated_at
            from user_title_state us
            left join anime a on a.id = us.anime_id
            order by coalesce(a.title, ''), us.anime_id
            """
        )
    ]


def write_user_state_sql(con, out_dir):
    rows = con.execute(
        """
        select anime_id, is_favorite, progress_episode_number, watched, updated_at
        from user_title_state
        order by anime_id
        """
    ).fetchall()
    dump = sqlite3.connect(":memory:")
    dump.execute(
        """
        create table user_title_state (
            anime_id integer primary key,
            is_favorite integer not null default 0,
            progress_episode_number integer,
            watched integer not null default 0,
            updated_at text not null
        )
        """
    )
    dump.executemany(
        "insert into user_title_state values (?, ?, ?, ?, ?)",
        [tuple(row) for row in rows],
    )
    sql = "\n".join(dump.iterdump()) + "\n"
    dump.close()
    path = out_dir / "user_title_state.sql"
    path.write_text(sql, encoding="utf-8")
    return path


def write_user_state_json(rows, out_dir):
    path = out_dir / "user_title_state.json"
    path.write_text(json.dumps(rows, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    return path


def write_user_state_csv(rows, out_dir):
    path = out_dir / "user_title_state.csv"
    fields = [
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
        writer = csv.DictWriter(handle, fieldnames=fields)
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
        rel = item.relative_to(ROOT)
        lines.append(f"{sha256_file(item)}  {rel}")
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
        "favorites": scalar(con, "select count(*) from user_title_state where is_favorite = 1"),
        "titles_with_progress": scalar(con, "select count(*) from user_title_state where progress_episode_number is not null"),
        "watched_titles": scalar(con, "select count(*) from user_title_state where watched = 1"),
    }


def write_readme(out_dir, created_at, counts):
    path = out_dir / "README.md"
    body = f"""# Current Backup

Created: {created_at}

This directory is the committed recovery snapshot for the current local catalog
state.

## Contents

- `animego.sqlite` - full SQLite backup made with SQLite's backup API.
- `user_title_state.sql` - SQL dump of local favorites/progress/watched state.
- `user_title_state.json` - readable user-state export with title/source data.
- `user_title_state.csv` - spreadsheet-friendly user-state export.
- `SHA256SUMS` - checksums for the active database and backup/export files.

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
- Favorites: {counts['favorites']}.
- Titles with progress: {counts['titles_with_progress']}.
- Watched titles: {counts['watched_titles']}.

## Restore Full Database

From the project root:

```bash
cp backups/current/animego.sqlite data/animego.sqlite
sqlite3 data/animego.sqlite 'pragma integrity_check;'
```

## Restore Only User State

From the project root:

```bash
sqlite3 data/animego.sqlite < backups/current/user_title_state.sql
```

The full backup and active database can have different SHA-256 hashes because
SQLite backup can rewrite page layout while preserving the logical database.
"""
    path.write_text(body, encoding="utf-8")
    return path


def update_backup(args):
    db_path = Path(args.db).resolve()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    con = connect(db_path)
    rows = user_state_rows(con)
    counts = collect_counts(con, db_path)
    con.close()

    created_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    backup_path = backup_database(db_path, out_dir)
    sql_con = connect(db_path)
    sql_path = write_user_state_sql(sql_con, out_dir)
    sql_con.close()
    json_path = write_user_state_json(rows, out_dir)
    csv_path = write_user_state_csv(rows, out_dir)
    readme_path = write_readme(out_dir, created_at, counts)
    checksum_path = write_checksums([db_path, backup_path, sql_path, json_path, csv_path], out_dir)
    print(f"wrote {backup_path}")
    print(f"wrote {sql_path}")
    print(f"wrote {json_path}")
    print(f"wrote {csv_path}")
    print(f"wrote {readme_path}")
    print(f"wrote {checksum_path}")


def main():
    parser = argparse.ArgumentParser(description="Refresh backups/current from the active SQLite database.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    update_backup(parser.parse_args())


if __name__ == "__main__":
    main()
