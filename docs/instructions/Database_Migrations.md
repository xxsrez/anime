# Database Migrations

Use `scripts/db_migrate.py` for ordered SQLite schema and data migrations.

Migrations are for one-off DDL and DML changes: schema changes, static seed
data, backfills, cleanups, and targeted user/data fixes. Rollback scripts are
not supported yet; rollback is a manual restore from backup.

## Layout

Migration scripts live in tracked source, not in ignored `db/` mutable state:

```text
migrations/
  2015-10-19-upgrade-to-pro/
    00_Add-GGPoker-Mystery-Battle-Royale-ICM-2331.sql
    01_Fix-GGPoker-Mystery-Battle-Royale-ICM-2331.sql
```

The runner applies first-level folders in lexicographic order, then `.sql`
files within each folder in lexicographic order. Folder names should start with
a date, but the runner intentionally sorts by name and does not parse the date.

## Commands

Inspect pending migrations:

```bash
.venv/bin/python scripts/db_migrate.py plan --db db/animego.sqlite
```

Apply pending migrations locally:

```bash
.venv/bin/python scripts/db_migrate.py apply --db db/animego.sqlite
```

Autostart is available but disabled by default. Set this only when startup-time
database mutation is intended:

```text
ANIME_AUTO_MIGRATE=1
ANIME_MIGRATIONS_ROOT=migrations
ANIME_MIGRATION_BACKUP_DIR=/data/backups
```

Check status for automation:

```bash
.venv/bin/python scripts/db_migrate.py status --db db/animego.sqlite
```

`status` returns `0` when up to date, `1` when migrations are pending, and `2`
when an applied script has checksum drift.

## Production

Production database changes remain explicit release operations. Prefer a manual
Railway SSH run after code containing the migration files has been deployed:

```bash
railway ssh --service web --environment production '
  python3 scripts/db_migrate.py apply \
    --db "${ANIMEGO_DB:-/data/animego.sqlite}" \
    --root migrations \
    --backup-dir /data/backups \
    --wait-lock
'
```

The runner creates a pre-migration SQLite backup unless `--no-backup` is passed,
uses a file lock next to the database, applies each SQL file in one transaction,
and then runs `pragma integrity_check` plus `pragma foreign_key_check`.

## Safety Rules

- Do not edit an already applied migration. Add a later migration instead.
- If a file with the same relative path has a different SHA-256 checksum, the
  runner stops before applying anything.
- Do not include transaction control in migration files. The runner owns
  transaction boundaries so each file is atomic with its history row.
- Historical files may be absent from a partial deployment bundle; the runner
  warns about history rows that are not present under the current migration
  root but does not fail on them.
