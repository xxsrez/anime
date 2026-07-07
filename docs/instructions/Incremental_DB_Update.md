# Incremental Database Updates

This is the durable workflow for adding titles, refreshing new episodes, and
making small catalog/player changes in production.

## Core Rule

Do not upload the whole SQLite database for normal catalog updates.

The Railway production database at `/data/animego.sqlite` is the source of
truth for production user state. Routine updates must run next to that file
inside the Railway service and mutate only the needed rows.

Full database upload/download is reserved for disaster recovery or a deliberate
full restore.

## Why

The database is already large enough that blob upload/download is slow. More
importantly, replacing `/data/animego.sqlite` from a local copy can overwrite
fresh production `user_title_state` rows. The safe path is:

1. Execute a small updater inside Railway via `railway ssh`.
2. Back up the current production DB inside `/data/backups`.
3. Run `sync_videos.py` against `/data/animego.sqlite`.
4. Verify SQLite integrity and playable-catalog invariants.

## Script Inventory

| Script | Role in DB updates |
| --- | --- |
| `sync_videos.py` | Main incremental updater. Supports feed/ongoing sync plus manual `--yummy-ref` and `--animego-ref` updates. Uses a lock file and per-title savepoints/commits. |
| `scrape_yummyanime.py` | YummyAnime/YummyAni parser and DB upsert helpers. Modern `ru.yummyani.me` rows use a separate id range from legacy `yummyanime.tv`. |
| `scrape_animego.py` | AnimeGO parser, DB schema initializer, and shared upsert helpers. |
| `backfill_players.py` | Finds rows with missing/partial playable coverage for backfill. |
| `prune_non_playable.py` | Removes metadata-only rows from the watchable catalog. |
| `update_backup.py` | Updates the local ignored `db/backups/current` snapshot. It is for local recovery, not production incremental updates. |
| `scripts/prod_incremental_update.py` | Local wrapper that runs the production updater inside Railway via SSH, with remote backup and health checks. |
| `scripts/check_data_health.py` | Local health check for a SQLite DB plus local backup checks. |
| `scripts/railway_start.sh` | Production start command; serves `server.py` using `ANIMEGO_DB` or `/data/animego.sqlite`. |
| `scripts/smoke_dev_app.py` | Local smoke test for auth gates, catalog API, and recommendations. |

## Manual Title Add

Use this for a specific YummyAni URL:

```bash
.venv/bin/python scripts/prod_incremental_update.py \
  --yummy-ref https://ru.yummyani.me/catalog/item/vanpanchmen-2 \
  --stop-on-error
```

Use this for a specific AnimeGO URL:

```bash
.venv/bin/python scripts/prod_incremental_update.py \
  --animego-ref https://animego.me/anime/vanpanchmen-3-2854 \
  --stop-on-error
```

The wrapper defaults to `--mode manual` when refs are provided. It does not
deploy code. If the updater code changed, deploy code first, then run the data
mutation.

## New Episodes / Routine Refresh

Use hourly mode for a small update pass:

```bash
.venv/bin/python scripts/prod_incremental_update.py \
  --mode hourly \
  --source yummyanime \
  --source animego \
  --stop-on-error
```

Hourly mode is intended for frequent use. It checks recent YummyAni feed items,
ongoing AnimeGO listings, and known rows with missing/partial coverage. The
updater skips already-known playable providers unless `--refresh-known` is set.

Use `--dry-run` before changing the production DB:

```bash
.venv/bin/python scripts/prod_incremental_update.py \
  --mode hourly \
  --source yummyanime \
  --source animego \
  --dry-run
```

## Direct Remote Command

The wrapper ultimately runs a command shaped like this:

```bash
railway ssh --service web --environment production '
  db="${ANIMEGO_DB:-/data/animego.sqlite}"
  python3 sync_videos.py --db "$db" --mode manual --wait-lock \
    --yummy-ref https://ru.yummyani.me/catalog/item/vanpanchmen-2
'
```

Prefer the wrapper because it also creates a backup and verifies the DB.

## Production Backup Policy

Before a non-dry-run production mutation, `scripts/prod_incremental_update.py`
creates a SQLite backup inside the Railway volume:

```text
/data/backups/animego-pre-incremental-YYYYMMDDTHHMMSSZ.sqlite
```

The wrapper keeps the latest five backups by default. Use `--keep-backups N` to
change that count.

These backups are on the Railway volume. They are not git artifacts and are not
the same as local `db/backups/current`.

## Verification Gate

After every production mutation, the wrapper checks:

- `pragma integrity_check`
- `pragma foreign_key_check`
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
| Only new title/episode data is needed and current prod code supports it | Run `scripts/prod_incremental_update.py`; no code deploy. |
| New updater flag or parser fix is required for the data update | Deploy code first, then run `scripts/prod_incremental_update.py`. |
| Local DB should be refreshed from production | Download a production backup/copy intentionally; do not assume local DB is fresher. |
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
3. For data-only updates, use `scripts/prod_incremental_update.py`.
4. Do not use `railway volume files upload ... /animego.sqlite` for routine
   title/episode updates.
5. Preserve production `user_title_state`; never replace prod DB with an older
   local DB just because local tests passed.
6. Report whether code was deployed, data was mutated, both, or neither.
