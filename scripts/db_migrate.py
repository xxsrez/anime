#!/usr/bin/env python3
import argparse
from dataclasses import dataclass
import datetime as dt
import fcntl
import hashlib
from pathlib import Path
import re
import sqlite3
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "db" / "animego.sqlite"
DEFAULT_MIGRATIONS = ROOT / "migrations"
HISTORY_TABLE = "schema_migrations"

LINE_COMMENT_RE = re.compile(r"--[^\n]*(?=\n|$)")
BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
TRANSACTION_CONTROL_RE = re.compile(r"^(begin|commit|end|rollback|savepoint|release)\b", re.I)


class MigrationError(RuntimeError):
    pass


class MigrationDriftError(MigrationError):
    pass


@dataclass(frozen=True)
class Migration:
    path: str
    folder: str
    filename: str
    full_path: Path
    checksum_sha256: str
    size_bytes: int


class FileLock:
    def __init__(self, path, wait=False):
        self.path = Path(path)
        self.wait = wait
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("w", encoding="utf-8")
        flags = fcntl.LOCK_EX if self.wait else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(self.handle, flags)
        except BlockingIOError as exc:
            raise MigrationError(f"migration lock is already held: {self.path}") from exc
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.handle:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def utc_stamp():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def connect_existing(db_path):
    db_path = Path(db_path)
    if not db_path.exists():
        raise MigrationError(f"database does not exist: {db_path}")
    con = sqlite3.connect(db_path, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("pragma foreign_keys=on")
    con.execute("pragma busy_timeout=30000")
    return con


def history_table_exists(con):
    return bool(
        con.execute(
            "select 1 from sqlite_master where type = 'table' and name = ?",
            (HISTORY_TABLE,),
        ).fetchone()
    )


def ensure_history_table(con):
    con.execute(
        f"""
        create table if not exists {HISTORY_TABLE} (
            path text primary key,
            folder text not null,
            filename text not null,
            checksum_sha256 text not null,
            size_bytes integer not null,
            applied_at text not null,
            duration_ms integer not null,
            status text not null default 'applied'
        )
        """
    )


def read_history(con):
    if not history_table_exists(con):
        return {}
    rows = con.execute(
        f"""
        select path, folder, filename, checksum_sha256, size_bytes, applied_at,
               duration_ms, status
        from {HISTORY_TABLE}
        order by path
        """
    ).fetchall()
    return {row["path"]: dict(row) for row in rows}


def file_checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_roots(roots):
    if roots is None:
        return [DEFAULT_MIGRATIONS]
    if isinstance(roots, (str, Path)):
        return [Path(roots)]
    return [Path(root) for root in roots]


def discover_migrations(root):
    root = Path(root)
    if not root.exists():
        return []
    migrations = []
    folders = (
        path for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")
    )
    for folder in sorted(folders, key=lambda path: path.name):
        scripts = (
            path
            for path in folder.iterdir()
            if path.is_file() and path.suffix.lower() == ".sql" and not path.name.startswith(".")
        )
        for script in sorted(scripts, key=lambda path: path.name):
            rel_path = f"{folder.name}/{script.name}"
            migrations.append(
                Migration(
                    path=rel_path,
                    folder=folder.name,
                    filename=script.name,
                    full_path=script,
                    checksum_sha256=file_checksum(script),
                    size_bytes=script.stat().st_size,
                )
            )
    return migrations


def discover_migrations_from_roots(roots):
    migrations = []
    seen = {}
    for root in normalize_roots(roots):
        for migration in discover_migrations(root):
            previous = seen.get(migration.path)
            if previous is not None:
                raise MigrationError(
                    f"duplicate migration path {migration.path}: "
                    f"{previous} and {migration.full_path}"
                )
            seen[migration.path] = migration.full_path
            migrations.append(migration)
    return migrations


def strip_comments(sql):
    return LINE_COMMENT_RE.sub("", BLOCK_COMMENT_RE.sub("", sql))


def has_effective_sql(sql):
    return bool(strip_comments(sql).strip())


def iter_sql_statements(sql):
    buffer = []
    for char in sql:
        buffer.append(char)
        statement = "".join(buffer)
        if sqlite3.complete_statement(statement):
            if has_effective_sql(statement):
                yield statement
            buffer = []
    remaining = "".join(buffer)
    if has_effective_sql(remaining):
        yield remaining


def assert_runner_owns_transaction(statement, migration):
    stripped = strip_comments(statement).lstrip()
    if TRANSACTION_CONTROL_RE.match(stripped):
        raise MigrationError(
            f"{migration.path} contains transaction control; "
            "migration files must not use BEGIN/COMMIT/ROLLBACK/SAVEPOINT"
        )


def collect_plan(db_path, roots):
    migrations = discover_migrations_from_roots(roots)
    con = connect_existing(db_path)
    try:
        history = read_history(con)
    finally:
        con.close()

    applied = []
    pending = []
    drift = []
    seen_paths = set()
    for migration in migrations:
        seen_paths.add(migration.path)
        row = history.get(migration.path)
        if row is None:
            pending.append(migration)
        elif row["checksum_sha256"] != migration.checksum_sha256:
            drift.append((migration, row["checksum_sha256"]))
        else:
            applied.append(migration)

    missing_files = sorted(path for path in history if path not in seen_paths)
    return {
        "applied": applied,
        "pending": pending,
        "drift": drift,
        "missing_files": missing_files,
        "history_count": len(history),
        "migration_count": len(migrations),
    }


def fail_on_drift(plan):
    if not plan["drift"]:
        return
    lines = ["applied migration checksum changed:"]
    for migration, old_checksum in plan["drift"]:
        lines.append(
            f"  {migration.path}: db={old_checksum} file={migration.checksum_sha256}"
        )
    raise MigrationDriftError("\n".join(lines))


def backup_database(db_path, backup_dir):
    db_path = Path(db_path)
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_stem = f"{db_path.stem}-pre-migrate-{utc_stamp()}"
    backup_path = backup_dir / f"{backup_stem}.sqlite"
    counter = 1
    while backup_path.exists():
        backup_path = backup_dir / f"{backup_stem}-{counter}.sqlite"
        counter += 1
    source = sqlite3.connect(db_path)
    target = sqlite3.connect(backup_path)
    try:
        with target:
            source.backup(target)
    finally:
        target.close()
        source.close()
    return backup_path


def apply_migration(con, migration):
    sql = migration.full_path.read_text(encoding="utf-8")
    started = time.perf_counter()
    con.execute("begin immediate")
    try:
        for statement in iter_sql_statements(sql):
            assert_runner_owns_transaction(statement, migration)
            con.execute(statement)
        duration_ms = max(0, int((time.perf_counter() - started) * 1000))
        con.execute(
            f"""
            insert into {HISTORY_TABLE} (
                path, folder, filename, checksum_sha256, size_bytes,
                applied_at, duration_ms, status
            ) values (?, ?, ?, ?, ?, ?, ?, 'applied')
            """,
            (
                migration.path,
                migration.folder,
                migration.filename,
                migration.checksum_sha256,
                migration.size_bytes,
                now_iso(),
                duration_ms,
            ),
        )
        con.execute("commit")
    except Exception:
        con.execute("rollback")
        raise
    return duration_ms


def verify_database(db_path):
    con = connect_existing(db_path)
    try:
        integrity = con.execute("pragma integrity_check").fetchone()[0]
        if integrity != "ok":
            raise MigrationError(f"integrity_check failed: {integrity}")
        fk_errors = con.execute("pragma foreign_key_check").fetchall()
        if fk_errors:
            raise MigrationError(f"foreign_key_check failed: {fk_errors[:5]}")
    finally:
        con.close()


def apply_pending(
    db_path,
    roots,
    backup_dir=None,
    no_backup=False,
    lock_path=None,
    wait_lock=False,
    verify=True,
):
    db_path = Path(db_path)
    lock_path = Path(lock_path) if lock_path else Path(f"{db_path}.migrate.lock")

    with FileLock(lock_path, wait=wait_lock):
        plan = collect_plan(db_path, roots)
        fail_on_drift(plan)
        pending = plan["pending"]
        if not pending:
            if verify:
                verify_database(db_path)
            return {"applied": [], "backup": None, "missing_files": plan["missing_files"]}

        backup_path = None
        if not no_backup:
            backup_path = backup_database(db_path, backup_dir or db_path.parent / "backups")

        con = connect_existing(db_path)
        try:
            ensure_history_table(con)
            applied = []
            for migration in pending:
                duration_ms = apply_migration(con, migration)
                applied.append((migration, duration_ms))
        finally:
            con.close()

        if verify:
            verify_database(db_path)
        return {"applied": applied, "backup": backup_path, "missing_files": plan["missing_files"]}


def print_plan(plan):
    fail_on_drift(plan)
    print(f"migrations: {plan['migration_count']}, history: {plan['history_count']}")
    if plan["pending"]:
        print("pending:")
        for migration in plan["pending"]:
            print(f"  {migration.path}")
    else:
        print("pending: none")
    if plan["missing_files"]:
        print("applied but not present under migration root:")
        for path in plan["missing_files"]:
            print(f"  {path}")


def print_status(db_path, roots):
    plan = collect_plan(db_path, roots)
    if plan["drift"]:
        print("status: drift")
        for migration, old_checksum in plan["drift"]:
            print(f"  {migration.path}: db={old_checksum} file={migration.checksum_sha256}")
        return 2
    if plan["pending"]:
        print(f"status: pending ({len(plan['pending'])})")
        for migration in plan["pending"]:
            print(f"  {migration.path}")
        return 1
    print("status: up to date")
    if plan["missing_files"]:
        print(f"note: {len(plan['missing_files'])} applied migration(s) are not present under this root")
    return 0


def add_common_args(parser):
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument(
        "--root",
        dest="roots",
        action="append",
        help="migration root directory; pass multiple times to apply multiple roots in order",
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Apply ordered SQLite migration scripts.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan", help="show pending migrations without changing the DB")
    add_common_args(plan_parser)

    status_parser = subparsers.add_parser("status", help="return non-zero when pending or drift exists")
    add_common_args(status_parser)

    apply_parser = subparsers.add_parser("apply", help="apply pending migrations")
    add_common_args(apply_parser)
    apply_parser.add_argument("--backup-dir", help="directory for pre-migration SQLite backups")
    apply_parser.add_argument("--no-backup", action="store_true", help="skip the pre-migration backup")
    apply_parser.add_argument("--lock-file", help="override migration lock path")
    apply_parser.add_argument("--wait-lock", action="store_true")
    apply_parser.add_argument("--no-verify", action="store_true", help="skip integrity and foreign-key checks")

    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        if args.command == "plan":
            print_plan(collect_plan(args.db, args.roots))
            return 0
        if args.command == "status":
            return print_status(args.db, args.roots)
        if args.command == "apply":
            result = apply_pending(
                args.db,
                args.roots,
                backup_dir=args.backup_dir,
                no_backup=args.no_backup,
                lock_path=args.lock_file,
                wait_lock=args.wait_lock,
                verify=not args.no_verify,
            )
            if result["backup"]:
                print(f"backup: {result['backup']}")
            if result["applied"]:
                print("applied:")
                for migration, duration_ms in result["applied"]:
                    print(f"  {migration.path} ({duration_ms} ms)")
            else:
                print("applied: none")
            if result["missing_files"]:
                print(f"note: {len(result['missing_files'])} applied migration(s) are not present under this root")
            return 0
        raise AssertionError(f"unsupported command: {args.command}")
    except MigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
