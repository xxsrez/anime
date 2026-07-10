#!/usr/bin/env python3
import argparse
from dataclasses import dataclass
import datetime as dt
import hashlib
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time

try:
    from scripts.operation_lock import DatabaseOperationLock, OperationLockError, default_lock_path
except ModuleNotFoundError:  # Direct execution: python3 scripts/db_migrate.py
    from operation_lock import DatabaseOperationLock, OperationLockError, default_lock_path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "db" / "animego.sqlite"
DEFAULT_MIGRATIONS = ROOT / "migrations"
HISTORY_TABLE = "schema_migrations"

LINE_COMMENT_RE = re.compile(r"--[^\n]*(?=\n|$)")
BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
TRANSACTION_CONTROL_RE = re.compile(r"^(begin|commit|end|rollback|savepoint|release)\b", re.I)
RUNTIME_SCHEMA_CONTRACT_RE = re.compile(
    r"^\s*--\s*runtime-schema-contract:\s*([a-z0-9][a-z0-9._-]*)\s*$",
    re.I | re.M,
)


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


class FileLock(DatabaseOperationLock):
    def __init__(self, path, wait=False, timeout=30.0):
        super().__init__("", path=path, wait=wait, timeout=timeout, operation="database migration")

    def __enter__(self):
        try:
            return super().__enter__()
        except OperationLockError as exc:
            raise MigrationError(f"migration lock is already held: {self.path}") from exc


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
        if char != ";":
            continue
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


def copy_database(source_path, target_path):
    source = sqlite3.connect(source_path)
    target = sqlite3.connect(target_path)
    try:
        with target:
            source.backup(target)
    finally:
        target.close()
        source.close()


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
    copy_database(db_path, backup_path)
    return backup_path


def apply_migration_body(con, migration):
    sql = migration.full_path.read_text(encoding="utf-8")
    started = time.perf_counter()
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
    return duration_ms


def migration_runtime_schema_contract(migration):
    header = migration.full_path.read_text(encoding="utf-8")[:4096]
    match = RUNTIME_SCHEMA_CONTRACT_RE.search(header)
    return match.group(1).lower() if match else None


def user_library_state_v1_satisfied(con):
    table = con.execute(
        "select 1 from sqlite_master where type = 'table' and name = 'user_title_state'"
    ).fetchone()
    if not table:
        return False
    columns = {row[1] for row in con.execute("pragma table_info(user_title_state)")}
    required_columns = {
        "user_id",
        "anime_id",
        "is_favorite",
        "progress_episode_number",
        "watched",
        "watch_status",
        "not_interested",
        "updated_at",
        "favorite_updated_at",
        "watch_status_updated_at",
        "not_interested_updated_at",
    }
    if not required_columns <= columns:
        return False
    indexes = {row[1] for row in con.execute("pragma index_list(user_title_state)")}
    if not {
        "idx_user_title_state_user_watch_status",
        "idx_user_title_state_user_not_interested",
    } <= indexes:
        return False
    # Finish the idempotent data part of the contract before recording it.  The
    # SQL file itself cannot conditionally skip SQLite ADD COLUMN statements,
    # so a full runtime-created shape is adopted and repaired here instead.
    con.execute(
        """
        update user_title_state
        set watch_status = case
                when watched = 1 then 'completed'
                when progress_episode_number is not null then 'watching'
                else watch_status
            end,
            favorite_updated_at = case
                when is_favorite = 1 then coalesce(favorite_updated_at, updated_at)
                else favorite_updated_at
            end,
            watch_status_updated_at = case
                when watch_status is not null or watched = 1 or progress_episode_number is not null
                then coalesce(watch_status_updated_at, updated_at)
                else watch_status_updated_at
            end,
            not_interested_updated_at = case
                when not_interested = 1 then coalesce(not_interested_updated_at, updated_at)
                else not_interested_updated_at
            end
        where (watch_status is null and (watched = 1 or progress_episode_number is not null))
           or (is_favorite = 1 and favorite_updated_at is null)
           or (watch_status is not null and watch_status_updated_at is null)
           or (not_interested = 1 and not_interested_updated_at is null)
        """
    )
    return True


RUNTIME_SCHEMA_CONTRACTS = {
    "user-library-state-v1": {
        "path": "2026-07-09_zzzzz-user-library-state/00_add_user_library_state.sql",
        "canonical_file": DEFAULT_MIGRATIONS
        / "2026-07-09_zzzzz-user-library-state"
        / "00_add_user_library_state.sql",
        "checker": user_library_state_v1_satisfied,
    },
}


def runtime_schema_contract_satisfied(con, migration):
    name = migration_runtime_schema_contract(migration)
    contract = RUNTIME_SCHEMA_CONTRACTS.get(name)
    if not contract or migration.path != contract["path"]:
        return False
    canonical_file = contract["canonical_file"]
    if not canonical_file.is_file() or migration.checksum_sha256 != file_checksum(canonical_file):
        return False
    return bool(contract["checker"](con))


def record_adopted_migration(con, migration):
    con.execute(
        f"""
        insert into {HISTORY_TABLE} (
            path, folder, filename, checksum_sha256, size_bytes,
            applied_at, duration_ms, status
        ) values (?, ?, ?, ?, ?, ?, 0, 'applied')
        """,
        (
            migration.path,
            migration.folder,
            migration.filename,
            migration.checksum_sha256,
            migration.size_bytes,
            now_iso(),
        ),
    )


def adopt_runtime_satisfied_migrations(db_path, pending):
    if not pending:
        return []
    con = connect_existing(db_path)
    try:
        con.execute("begin immediate")
        adopted = [
            migration
            for migration in pending
            if runtime_schema_contract_satisfied(con, migration)
        ]
        if not adopted:
            con.execute("rollback")
            return []
        ensure_history_table(con)
        for migration in adopted:
            record_adopted_migration(con, migration)
        con.execute("commit")
        return adopted
    except Exception:
        if con.in_transaction:
            con.execute("rollback")
        raise
    finally:
        con.close()


def verify_connection(con):
    integrity = con.execute("pragma integrity_check").fetchone()[0]
    if integrity != "ok":
        raise MigrationError(f"integrity_check failed: {integrity}")
    fk_errors = con.execute("pragma foreign_key_check").fetchall()
    if fk_errors:
        raise MigrationError(f"foreign_key_check failed: {[tuple(row) for row in fk_errors[:5]]}")


def verify_database(db_path):
    con = connect_existing(db_path)
    try:
        verify_connection(con)
    finally:
        con.close()


def verify_quick_integrity(db_path):
    """Reject obvious source damage before spending work on a snapshot.

    The exact candidate and final live result receive full integrity and FK
    checks later. Repeating a full source scan here has no distinct safety
    value because the candidate is copied from this same locked database.
    """
    con = connect_existing(db_path)
    try:
        integrity = con.execute("pragma quick_check(1)").fetchone()[0]
        if integrity != "ok":
            raise MigrationError(f"quick_check failed: {integrity}")
    finally:
        con.close()


def preflight_migrations(candidate_db, pending):
    """Apply and fully verify a batch on an isolated snapshot, then roll it back.

    The rollback leaves a recovery backup logically unchanged, so the same
    locked SQLite snapshot can safely serve both recovery and preflight roles.
    Callers that opt out of a backup provide a disposable temporary snapshot.
    """
    con = connect_existing(candidate_db)
    try:
        con.execute("begin immediate")
        try:
            ensure_history_table(con)
            for migration in pending:
                apply_migration_body(con, migration)
            verify_connection(con)
        finally:
            if con.in_transaction:
                con.execute("rollback")
    finally:
        con.close()


def apply_pending(
    db_path,
    roots,
    backup_dir=None,
    no_backup=False,
    lock_path=None,
    wait_lock=False,
    lock_timeout=30.0,
    verify=True,
):
    db_path = Path(db_path)
    lock_path = Path(lock_path) if lock_path else default_lock_path(db_path)

    with FileLock(lock_path, wait=wait_lock, timeout=lock_timeout):
        plan = collect_plan(db_path, roots)
        fail_on_drift(plan)
        pending = plan["pending"]
        if not pending:
            if verify:
                verify_database(db_path)
            return {
                "applied": [],
                "adopted": [],
                "backup": None,
                "missing_files": plan["missing_files"],
            }

        # Snapshot before contract adoption: adoption repairs data and writes
        # migration history, so it is part of the migration operation too.
        if verify:
            verify_quick_integrity(db_path)
        backup_path = None
        if not no_backup:
            backup_path = backup_database(db_path, backup_dir or db_path.parent / "backups")

        adopted = adopt_runtime_satisfied_migrations(db_path, pending)
        if adopted:
            adopted_paths = {migration.path for migration in adopted}
            pending = [migration for migration in pending if migration.path not in adopted_paths]
        if not pending:
            if verify:
                verify_database(db_path)
            return {
                "applied": [],
                "adopted": adopted,
                "backup": backup_path,
                "missing_files": plan["missing_files"],
            }

        # The checks have distinct roles: a cheap source quick-check rejects
        # obvious damage, a full candidate check proves the complete batch and
        # its FKs before live writes, and a final full check proves the actual
        # live result before commit. FK damage in the source may deliberately be
        # repaired by a pending migration, so it is checked after the batch.
        if verify:
            if backup_path is not None:
                # The transaction in preflight_migrations is always rolled
                # back, preserving this exact pre-migration recovery snapshot.
                preflight_migrations(backup_path, pending)
            else:
                with tempfile.TemporaryDirectory(prefix="anime-migration-preflight-") as tmpdir:
                    candidate = Path(tmpdir) / "candidate.sqlite"
                    copy_database(db_path, candidate)
                    preflight_migrations(candidate, pending)

        con = connect_existing(db_path)
        try:
            applied = []
            con.execute("begin immediate")
            try:
                ensure_history_table(con)
                for migration in pending:
                    duration_ms = apply_migration_body(con, migration)
                    applied.append((migration, duration_ms))
                if verify:
                    verify_connection(con)
                con.execute("commit")
            except Exception:
                con.execute("rollback")
                raise
        finally:
            con.close()
        return {
            "applied": applied,
            "adopted": adopted,
            "backup": backup_path,
            "missing_files": plan["missing_files"],
        }


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
    apply_parser.add_argument("--lock-timeout", type=float, default=30.0)
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
                lock_timeout=args.lock_timeout,
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
