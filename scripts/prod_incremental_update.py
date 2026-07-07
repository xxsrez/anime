#!/usr/bin/env python3
import argparse
import shlex
import subprocess
import sys
import textwrap


def shell_join(parts):
    return " ".join(shlex.quote(str(part)) for part in parts)


def build_sync_args(args):
    parts = [
        "python3",
        "sync_videos.py",
        "--db",
        "$db",
        "--mode",
        args.mode,
        "--wait-lock",
    ]
    for source in args.sources or []:
        parts.extend(["--source", source])
    for ref in args.yummy_refs:
        parts.extend(["--yummy-ref", ref])
    for ref in args.animego_refs:
        parts.extend(["--animego-ref", ref])
    if args.episode_limit is not None:
        parts.extend(["--episode-limit", str(args.episode_limit)])
    if args.refresh_known:
        parts.append("--refresh-known")
    if args.dry_run:
        parts.append("--dry-run")
    if args.stop_on_error:
        parts.append("--stop-on-error")
    if args.verbose:
        parts.append("--verbose")
    return shell_join(parts).replace("'$db'", '"$db"')


def build_remote_command(args):
    sync_command = build_sync_args(args)
    backup_dir = shlex.quote(args.backup_dir)
    keep = shlex.quote(str(args.keep_backups))
    blocks = [
        "set -eu",
        'db="${ANIMEGO_DB:-/data/animego.sqlite}"',
        'if [ ! -s "$db" ]; then',
        '  echo "missing database: $db" >&2',
        "  exit 1",
        "fi",
    ]
    if not args.dry_run and not args.no_backup:
        backup_python = textwrap.dedent(
            """
            import datetime as dt
            import os
            from pathlib import Path
            import sqlite3

            db_path = Path(os.environ["DB_PATH"])
            backup_dir = Path(os.environ["BACKUP_DIR"])
            keep = int(os.environ["BACKUP_KEEP"])
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_path = backup_dir / f"animego-pre-incremental-{stamp}.sqlite"
            source = sqlite3.connect(db_path)
            target = sqlite3.connect(backup_path)
            try:
                with target:
                    source.backup(target)
            finally:
                target.close()
                source.close()
            print(f"backup={backup_path}")
            backups = sorted(
                backup_dir.glob("animego-pre-incremental-*.sqlite"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            for old in backups[keep:]:
                old.unlink()
                print(f"pruned_backup={old}")
            """
        ).strip()
        blocks.extend(
            [
                f'DB_PATH="$db" BACKUP_DIR={backup_dir} BACKUP_KEEP={keep} python3 - <<\'PY\'',
                backup_python,
                "PY",
            ]
        )

    verify_python = textwrap.dedent(
        """
        import os
        import sqlite3

        db_path = os.environ["DB_PATH"]
        con = sqlite3.connect(db_path)
        try:
            integrity = con.execute("pragma integrity_check").fetchone()[0]
            if integrity != "ok":
                raise SystemExit(f"integrity_check failed: {integrity}")
            fk_errors = con.execute("pragma foreign_key_check").fetchall()
            if fk_errors:
                raise SystemExit(f"foreign_key_check failed: {fk_errors[:5]}")
            non_playable = con.execute(
                '''
                select count(*)
                from anime a
                where not exists (
                    select 1
                    from video_sources vs
                    where vs.anime_id = a.id
                      and vs.embed_url is not null
                )
                '''
            ).fetchone()[0]
            if non_playable:
                raise SystemExit(f"non-playable source rows: {non_playable}")
            counts = {
                "anime": con.execute("select count(*) from anime").fetchone()[0],
                "episodes": con.execute("select count(*) from episodes").fetchone()[0],
                "video_sources": con.execute(
                    "select count(*) from video_sources where embed_url is not null"
                ).fetchone()[0],
                "user_title_state": con.execute("select count(*) from user_title_state").fetchone()[0],
            }
        finally:
            con.close()
        print("incremental health ok: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
        """
    ).strip()
    blocks.extend(
        [
            sync_command,
            'DB_PATH="$db" python3 - <<\'PY\'',
            verify_python,
            "PY",
        ]
    )
    return "\n".join(blocks)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Run an incremental Anime production DB update inside Railway.")
    parser.add_argument("--service", default="web")
    parser.add_argument("--environment", default="production")
    parser.add_argument("--project")
    parser.add_argument("--mode", choices=["manual", "hourly", "daily", "full"], default=None)
    parser.add_argument("--source", dest="sources", action="append", choices=["animego", "yummyanime"])
    parser.add_argument("--yummy-ref", dest="yummy_refs", action="append", default=[])
    parser.add_argument("--animego-ref", dest="animego_refs", action="append", default=[])
    parser.add_argument("--episode-limit", type=int)
    parser.add_argument("--refresh-known", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--backup-dir", default="/data/backups")
    parser.add_argument("--keep-backups", type=int, default=5)
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--print-command", action="store_true")
    args = parser.parse_args(argv)
    if args.mode is None:
        args.mode = "manual" if args.yummy_refs or args.animego_refs else "hourly"
    if args.mode == "manual" and not (args.yummy_refs or args.animego_refs):
        parser.error("--mode manual requires at least one --yummy-ref or --animego-ref")
    if args.keep_backups < 1:
        parser.error("--keep-backups must be >= 1")
    return args


def main(argv=None):
    args = parse_args(argv)
    remote_command = build_remote_command(args)
    if args.print_command:
        print(remote_command)
        return 0

    command = ["railway", "ssh", "--service", args.service, "--environment", args.environment]
    if args.project:
        command.extend(["--project", args.project])
    command.append(remote_command)
    return subprocess.call(command)


if __name__ == "__main__":
    raise SystemExit(main())
