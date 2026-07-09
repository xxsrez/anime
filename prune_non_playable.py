#!/usr/bin/env python3
import argparse
import sqlite3
from pathlib import Path

from scripts.operation_lock import DatabaseOperationLock, default_lock_path


DEFAULT_DB = Path(__file__).resolve().parent / "db" / "animego.sqlite"


def connect(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("pragma foreign_keys=on")
    return con


def prepare_prune_ids(con, source=None):
    con.execute("drop table if exists temp.prune_anime_ids")
    con.execute("create temp table prune_anime_ids(id integer primary key)")
    params = []
    where = [
        """
        not exists (
            select 1
            from video_sources vs
            where vs.anime_id = a.id
              and vs.embed_url is not null
        )
        """
    ]
    if source:
        where.append("a.source = ?")
        params.append(source)
    con.execute(
        f"""
        insert into temp.prune_anime_ids(id)
        select a.id
        from anime a
        where {' and '.join(where)}
        """,
        params,
    )


def count_rows(con, table=None, column=None):
    if column:
        return con.execute(
            f"select count(*) from {table} where {column} in (select id from temp.prune_anime_ids)"
        ).fetchone()[0]
    return con.execute("select count(*) from temp.prune_anime_ids").fetchone()[0]


def summary(con):
    rows = con.execute(
        """
        select coalesce(a.source, 'unknown') source, count(*) count
        from anime a
        join temp.prune_anime_ids p on p.id = a.id
        group by coalesce(a.source, 'unknown')
        order by count desc, source
        """
    ).fetchall()
    return {row["source"]: row["count"] for row in rows}


def delete_rows(con):
    tables = (
        "user_episode_state",
        "user_watch_events",
        "content_update_events",
        "video_sources",
        "episodes",
        "anime_title_aliases",
        "anime_fields",
        "anime_genres",
        "anime_dubbings",
        "user_title_state",
    )
    existing = {
        row[0]
        for row in con.execute("select name from sqlite_master where type = 'table'")
    }
    for table in tables:
        if table in existing:
            con.execute(
                f"delete from {table} where anime_id in (select id from temp.prune_anime_ids)"  # noqa: S608 -- table is a fixed internal constant
            )
    con.execute("delete from anime where id in (select id from temp.prune_anime_ids)")


def _prune(args):
    con = connect(args.db)
    prepare_prune_ids(con, args.source)
    total = count_rows(con)
    print(f"non-playable anime rows: {total}")
    for source, count in summary(con).items():
        print(f"  {source}: {count}")
    print(f"related episodes: {count_rows(con, 'episodes', 'anime_id')}")
    print(f"related video_sources: {count_rows(con, 'video_sources', 'anime_id')}")
    print(f"related user_title_state: {count_rows(con, 'user_title_state', 'anime_id')}")

    if not args.commit:
        print("dry run only; pass --commit to delete")
        con.close()
        return total

    delete_rows(con)
    con.commit()
    con.close()
    print(f"deleted {total} non-playable anime rows")
    return total


def prune(args):
    lock_path = getattr(args, "lock_file", None) or default_lock_path(args.db)
    with DatabaseOperationLock(
        args.db,
        path=lock_path,
        wait=getattr(args, "wait_lock", False),
        timeout=getattr(args, "lock_timeout", 30.0),
        operation="prune non-playable titles",
    ):
        return _prune(args)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Remove source rows that have no playable video source.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--source", choices=["animego", "yummyanime"])
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--lock-file")
    parser.add_argument("--wait-lock", action="store_true")
    parser.add_argument("--lock-timeout", type=float, default=30.0)
    prune(parser.parse_args(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
