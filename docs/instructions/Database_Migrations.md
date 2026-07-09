# Database Migrations

Use `scripts/db_migrate.py` for ordered SQLite migrations.

Git-tracked migrations are only for license-clean schema/control changes:
schema changes, static seed data that we own, cleanups, and operational fixes.
Scraped catalog/player data patches are private deployment artifacts and must
stay outside git under an ignored root such as `data/private-migrations/`.
Rollback scripts are not supported yet; rollback is a manual restore from a
local backup or another explicit restore artifact.

## Layout

Tracked migration scripts live in source:

```text
migrations/
  2026-07-08_baseline/
    00_noop.sql
  2026-07-09_schema-change/
    00_add-owned-table.sql
```

Private generated data patches live outside git:

```text
data/private-migrations/
  2026-07-08_catalog-2020-2019/
    00_data-update-00.sql
    00_data-update-01.sql
```

The runner applies first-level folders in lexicographic order, then `.sql`
files within each folder in lexicographic order. Folder names should start with
a date, but the runner intentionally sorts by name and does not parse the date.
When multiple roots are passed, roots are applied in CLI order. If two roots
contain the same relative `folder/file` path, the runner fails before applying
anything because `schema_migrations.path` must be unambiguous.

## Commands

Inspect pending migrations:

```bash
.venv/bin/python scripts/db_migrate.py plan --db db/animego.sqlite
```

Apply pending migrations locally:

```bash
.venv/bin/python scripts/db_migrate.py apply --db db/animego.sqlite
```

Apply both tracked schema/control migrations and local private data patches:

```bash
.venv/bin/python scripts/db_migrate.py apply \
  --db db/animego.sqlite \
  --root migrations \
  --root data/private-migrations
```

Generate a data migration from a scraper/update run without mutating the source
database:

```bash
.venv/bin/python sync_videos.py \
  --db db/animego.sqlite \
  --mode manual \
  --yummy-ref https://ru.yummyani.me/catalog/item/example \
  --emit-migration 2026-07-08_example-title \
  --stop-on-error
```

`--emit-migration` copies the selected database to a scratch location, runs the
normal sync against that scratch copy, diffs catalog/player tables before and
after, and writes only real catalog/player data changes into
`data/private-migrations/` by default. Audit-only updates such as
`video_sync_runs` do not create a migration file.

Do not commit generated catalog/player SQL. It can contain scraped titles,
descriptions, embed URLs, and other copyright-sensitive source data. Keep it in
ignored local storage, then copy it to the production volume as a private data
artifact.

Autostart is available but disabled by default. Set this only when startup-time
database mutation is intended:

```text
ANIME_AUTO_MIGRATE=1
ANIME_MIGRATIONS_ROOTS=migrations:/data/private-migrations
ANIME_MIGRATION_NO_BACKUP=1
```

`ANIME_MIGRATIONS_ROOT` is still supported for a single root.

Check status for automation:

```bash
.venv/bin/python scripts/db_migrate.py status --db db/animego.sqlite
```

`status` returns `0` when up to date, `1` when migrations are pending, and `2`
when an applied script has checksum drift.

## Production

Production database changes remain explicit release operations. Deploy code and
tracked migrations with `railway up`, copy private data patches to the Railway
volume outside git, then run:

```bash
railway ssh --service web --environment production '
  python3 scripts/db_migrate.py apply \
    --db "${ANIMEGO_DB:-/data/animego.sqlite}" \
    --root migrations \
    --no-backup \
    --wait-lock \
    --lock-timeout 1800
'
```

Add `--root /data/private-migrations` only for a deliberate private data patch.
The runner uses a canonical file lock next to the database, preflights the
complete pending batch on a disposable snapshot, then applies the complete
batch atomically on the live database. Before commit it runs
`pragma integrity_check` and `pragma foreign_key_check`. Production deliberately
uses `--no-backup`; recovery artifacts remain local-only.

## Safety Rules

- Do not edit an already applied migration. Add a later migration instead.
- Do not put scraped catalog/player data patches in git. Keep them under
  `data/private-migrations/` locally and `/data/private-migrations/` on
  Railway.
- If a file with the same relative path has a different SHA-256 checksum, the
  runner stops before applying anything.
- Do not reuse the same relative `folder/file` path in different migration
  roots.
- Do not include transaction control in migration files. The runner owns
  transaction boundaries so the entire pending batch and all history rows are
  atomic.
- Historical files may be absent from a partial deployment bundle; the runner
  warns about history rows that are not present under the current migration
  root but does not fail on them.
