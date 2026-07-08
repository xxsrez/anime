# Incremental Database Updates

This is the durable workflow for adding titles, refreshing new episodes, and
making small catalog/player changes in production.

## Core Rule

Do not upload the whole SQLite database for normal catalog updates.

The local dev database and the Railway production database should normally move
in lockstep by applying the same ordered private SQL data patches locally and
then on production through `scripts/db_migrate.py`. Treat those patches as the
routine source of synchronization. Do not download or upload the whole
production SQLite file during normal releases unless there is concrete evidence
that local and production databases diverged or an explicit spot audit is
requested.

The Railway production database at `/data/animego.sqlite` is still the source
of truth for production user state. Direct in-place sync remains a fallback for
emergency or exploratory production operations, not the default release path.

Full database upload/download is reserved for disaster recovery or a deliberate
full restore.

## Why

The database is already large enough that blob upload/download is slow. More
importantly, replacing `/data/animego.sqlite` from a local copy can overwrite
fresh production `user_title_state` rows. The safe path is:

1. Start from the local dev database unless there is evidence of drift.
2. Generate a migration from a scratch sync using `sync_videos.py --emit-migration`.
3. Apply the generated private patch locally and verify dev.
4. Review the generated SQL locally, but keep catalog/player data outside git.
5. Commit and deploy only license-clean code plus tracked schema/control
   migrations.
6. Copy private data patches to `/data/private-migrations` on the Railway
   volume outside GitHub.
7. Apply both migration roots next to `/data/animego.sqlite` through
   `scripts/db_migrate.py`.
8. Verify SQLite integrity and playable-catalog invariants.

## Script Inventory

| Script | Role in DB updates |
| --- | --- |
| `sync_videos.py` | Main incremental updater. Supports feed/ongoing sync plus manual `--yummy-ref` and `--animego-ref` updates. `--emit-migration` writes catalog/player data changes into ignored private SQL patches instead of mutating the source DB. |
| `scrape_yummyanime.py` | YummyAnime/YummyAni parser and DB upsert helpers. Modern `ru.yummyani.me` rows use a separate id range from legacy `yummyanime.tv`. |
| `scrape_animego.py` | AnimeGO parser, DB schema initializer, and shared upsert helpers. |
| `backfill_players.py` | Finds rows with missing/partial playable coverage for backfill. |
| `prune_non_playable.py` | Removes metadata-only rows from the watchable catalog. |
| `update_backup.py` | Updates the local ignored `db/backups/current` snapshot. It is for local recovery, not production incremental updates. |
| `scripts/prod_incremental_update.py` | Legacy fallback wrapper for direct in-place production sync via Railway SSH. It does not create Railway backups; prefer migration generation for routine updates. |
| `scripts/db_migrate.py` | Applies ordered SQL migrations and records them in `schema_migrations`. |
| `scripts/check_data_health.py` | Local health check for a SQLite DB plus local backup checks. |
| `scripts/railway_start.sh` | Production start command; serves `server.py` using `ANIMEGO_DB` or `/data/animego.sqlite`. |
| `scripts/smoke_dev_app.py` | Local smoke test for auth gates, catalog API, and recommendations. |

## Manual Title Add

Use this for a specific YummyAni URL:

```bash
.venv/bin/python sync_videos.py \
  --db db/animego.sqlite \
  --mode manual \
  --yummy-ref https://ru.yummyani.me/catalog/item/vanpanchmen-2 \
  --emit-migration 2026-07-08_vanpanchmen-2 \
  --stop-on-error
```

Use this for a specific AnimeGO URL:

```bash
.venv/bin/python sync_videos.py \
  --db db/animego.sqlite \
  --mode manual \
  --animego-ref https://animego.me/anime/vanpanchmen-3-2854 \
  --emit-migration 2026-07-08_vanpanchmen-3 \
  --stop-on-error
```

Review the generated SQL locally, keep it under `data/private-migrations/`, and
do not commit it. If updater code changed, commit/deploy the code first, copy
the private patch to the Railway volume, then apply both migration roots.

## New Episodes / Routine Refresh

Use hourly mode for a small update pass and emit a migration:

```bash
.venv/bin/python sync_videos.py \
  --db db/animego.sqlite \
  --mode hourly \
  --source yummyanime \
  --source animego \
  --emit-migration 2026-07-08_hourly-catalog-refresh \
  --stop-on-error
```

Hourly mode is intended for frequent use. It checks recent YummyAni feed items,
ongoing AnimeGO listings, and known rows with missing/partial coverage. The
updater skips already-known playable providers unless `--refresh-known` is set.

Use `--dry-run` when checking updater reachability without writing a migration
or changing a database:

```bash
.venv/bin/python sync_videos.py \
  --db db/animego.sqlite \
  --mode hourly \
  --source yummyanime \
  --source animego \
  --dry-run
```

## Historical Year Backfill

Use manual mode with catalog-year selectors when adding older years. Generate
the migration locally; do not scrape historical years on production:

```bash
PYTHONUNBUFFERED=1 .venv/bin/python sync_videos.py \
  --db db/animego.sqlite \
  --mode manual \
  --source yummyanime \
  --source animego \
  --yummy-catalog-year 2020 \
  --yummy-catalog-year 2019 \
  --animego-season-year 2020 \
  --animego-season-year 2019 \
  --emit-migration 2026-07-08_catalog-2020-2019 \
  --retry-attempts 5 \
  --retry-backoff 3
```

Large historical runs can take tens of minutes because they fetch detail pages
and playable providers for many titles. Use `PYTHONUNBUFFERED=1` or redirect
output to a log so progress and failed URLs are visible. If a few URLs fail from
transient timeouts, rerun only those refs into a small follow-up migration.
Metadata-only rows should remain skipped unless a playable provider can be
verified.

## Copying Private Patches

Generated catalog/player patches are local private artifacts. Keep them under
`data/private-migrations/` and copy the specific folder to the Railway volume:

```bash
railway volume files -v web-volume upload \
  data/private-migrations/2026-07-08_catalog-2020-2019 \
  /private-migrations/2026-07-08_catalog-2020-2019 \
  --overwrite \
  --json
```

The remote volume root is mounted at `/data`, so the uploaded folder is visible
to the app container as `/data/private-migrations/2026-07-08_catalog-2020-2019`.
Do not upload the whole SQLite database for this workflow.

## Applying Generated Migrations

After deployment, apply pending migrations next to the Railway database:

```bash
railway ssh --service web --environment production '
  db="${ANIMEGO_DB:-/data/animego.sqlite}"
  python3 scripts/db_migrate.py apply \
    --db "$db" \
    --root migrations \
    --root /data/private-migrations \
    --no-backup \
    --wait-lock
'
```

`db_migrate.py` records applied files in `schema_migrations`. The tracked
`migrations/` root is for license-clean schema/control scripts. The
`/data/private-migrations` root is for copied private catalog/player patches.

## Backup Policy

Do not keep SQLite backup files on Railway. Production migration commands pass
`--no-backup`, and the Railway volume should only contain the live database,
logs, and private migration artifacts needed for replay. Keep recovery snapshots
locally under ignored paths such as `db/backups/` or another explicit local
restore folder.

## Verification Gate

After every production migration, the migration runner checks:

- `pragma integrity_check`
- `pragma foreign_key_check`

Then run the project catalog checks:

- zero source rows without playable `video_sources.embed_url`
- row counts for `anime`, `episodes`, playable `video_sources`, and
  `user_title_state`

Then smoke-check production:

```bash
curl -fsS https://anime-srez.up.railway.app/api/health
curl -sS -i https://anime-srez.up.railway.app/api/me
curl -sS -i https://anime-srez.up.railway.app/login
```

Expected:

- `/api/health` returns `{"ok": true}`
- `/api/me` returns `401` without a session
- `/login` returns the Google login page

Authenticated catalog checks need an existing browser session.

## Code Release vs Data Update

Use this decision table:

| Change | Required action |
| --- | --- |
| Parser/updater/server/frontend code changed | Run tests, commit, push/deploy code with `railway up`. |
| Only new title/episode data is needed and current code supports it | Generate a private SQL patch with `sync_videos.py --emit-migration`, copy it outside git to `/data/private-migrations`, then apply with `scripts/db_migrate.py`. |
| New updater flag or parser fix is required for the data update | Generate the private patch with the fixed code, deploy code only through git/Railway, copy the private patch to the volume, then apply both roots with `scripts/db_migrate.py`. |
| Local DB should be refreshed from production | Download a production copy intentionally only for an explicit spot audit or repair; do not do this as a routine release step. |
| Local/prod drift is suspected or a spot audit was explicitly requested | Download a production copy to ignored local storage, compare catalog/player signatures, then decide whether to repair with a private patch. |
| Production DB is corrupted or intentionally being replaced wholesale | Follow an explicit disaster-recovery restore plan; full upload is allowed only then. |

## Cache Behavior

`server.py` caches catalog data by database file signature: mtime plus size.
In-place production mutations change that signature, so a data-only incremental
update does not require a server restart for catalog visibility.

Restart/redeploy is still required after Python route/server code changes.

## AI Agent Checklist

For future AI agents:

1. Read this document and `Operations_Runbook.md` before touching production DB.
2. Decide whether the task is code release, data-only update, or disaster
   restore.
3. For data-only updates, generate a private patch with
   `sync_videos.py --emit-migration`, keep it out of git, and apply it with
   `scripts/db_migrate.py`.
4. Do not use `railway volume files upload ... /animego.sqlite` for routine
   title/episode updates.
5. Do not commit generated catalog/player SQL. It may contain scraped
   copyright-sensitive source data.
6. Apply each private patch locally before production so local and production
   catalog/player state stay synchronized without full DB transfers.
7. Preserve production `user_title_state`; never replace prod DB with an older
   local DB just because local tests passed.
8. Report whether code was deployed, data was mutated, both, or neither.
