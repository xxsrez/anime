# Railway Production Release

`../../instructions/Operations_Runbook.md` is the central source of truth for
the release checklist, verification, dev/prod boundaries, env vars, and
troubleshooting. This file is a Railway-specific quick reference.

Railway is the production environment for Anime Local. The retired localhost
production install at `127.0.0.1:8766` must not be used for releases.

## Production Target

- Project: `anime-local`
- Environment: `production`
- Service: `web`
- Public URL: `https://anime-srez.up.railway.app`
- Volume: `web-volume`
- Volume mount: `/data`
- Runtime config: `railway.json`
- Start command: `sh scripts/railway_start.sh`
- App database: `/data/animego.sqlite`
- Logs directory: `/data/logs`

`scripts/railway_start.sh` starts `server.py` with Railway's `$PORT` and the
SQLite path from `ANIMEGO_DB`, defaulting to `/data/animego.sqlite`.

## Required Variables

These variables must exist for the `web` service in the `production`
environment:

```text
GOOGLE_CLIENT_ID
ANIME_ADMIN_EMAIL
ANIME_SESSION_SECURE=1
ANIMEGO_DB=/data/animego.sqlite
ANIME_LOG_DIR=/data/logs
ANIME_SYNC_TOKEN
```

Optional access controls may also be set:

```text
ANIME_AUTH_ALLOWED_EMAILS
ANIME_AUTH_ALLOWED_DOMAINS
ANIME_SYNC_MODE=daily
```

If `ANIME_AUTH_ALLOWED_EMAILS` is set, it must include `ANIME_ADMIN_EMAIL`.
The admin email is environment-specific and should be set separately for each
Railway environment before release.

Use caution with:

```bash
railway variable list --json
railway variable list --kv
```

Those commands print raw variable values and should not be pasted into reports.
For key-only audits, pipe the JSON to a local parser and print keys only.

The Google OAuth client must include the Railway origin in Authorized
JavaScript origins:

```text
https://anime-srez.up.railway.app
```

## Release Checklist

Use the full checklist in `../../instructions/Operations_Runbook.md`. The
abbreviated Railway sequence is:

1. Verify the intended local state on dev:

```bash
.venv/bin/python -m py_compile server.py scrape_animego.py scrape_yummyanime.py sync_videos.py backfill_players.py prune_non_playable.py update_backup.py test_app.py scripts/check_repo_hygiene.py scripts/check_data_health.py scripts/smoke_dev_app.py scripts/db_migrate.py scripts/db_data_diff.py test_db_migrate.py
.venv/bin/python -m unittest -v test_app.py test_db_migrate.py
node --check static/app.js
node --check static/login.js
node --check static/admin.js
.venv/bin/python scripts/smoke_dev_app.py
```

2. Confirm Railway linkage:

```bash
railway status
railway volume list --json
```

3. For routine title/episode data changes, generate a private SQL data patch
   instead of uploading or replacing the whole SQLite file:

```bash
.venv/bin/python sync_videos.py \
  --db db/animego.sqlite \
  --mode hourly \
  --source yummyanime \
  --source animego \
  --emit-migration 2026-07-08_hourly-catalog-refresh \
  --stop-on-error
```

For a specific title:

```bash
.venv/bin/python sync_videos.py \
  --db db/animego.sqlite \
  --mode manual \
  --yummy-ref https://ru.yummyani.me/catalog/item/example \
  --emit-migration 2026-07-08_example-title \
  --stop-on-error
```

For older catalog years, generate the SQL locally, keep it outside git, and
apply it after deploy. Do not run the broad scraper inside Railway:

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
  --emit-migration 2026-07-08_catalog-2020-2019
```

`db/animego.sqlite` is ignored by git. It is not included in `railway up`.
Routine production data patches should stay under ignored
`data/private-migrations/`, be copied to the Railway volume, and be applied
through `scripts/db_migrate.py`; see
`../../instructions/Incremental_DB_Update.md`.

Do not download or upload the whole production SQLite database for routine
releases. Local and production catalog/player data should stay synchronized by
applying the same private patches locally first and then on Railway. Full
database transfers are for explicit spot audits, concrete drift evidence, or
disaster recovery.

Full upload is emergency-only for deliberate full restores:

```bash
railway volume files -v web-volume upload db/animego.sqlite /animego.sqlite --overwrite --json
```

4. Deploy the current working tree to Railway production:

```bash
railway up --service web --environment production --detach --message "production release"
```

`railway up` uploads from the current directory and respects `.gitignore`.
Use it only when the current working tree is the intended production code.

5. Copy private data patches to the Railway volume if production data should
   change:

```bash
railway volume files -v web-volume upload \
  data/private-migrations/2026-07-08_catalog-2020-2019 \
  /private-migrations/2026-07-08_catalog-2020-2019 \
  --overwrite \
  --json
```

6. Apply pending migrations if production data should change:

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

7. Watch deployment state and logs:

```bash
railway deployment list --json
railway logs --latest --lines 100
```

8. Smoke-check production:

```bash
curl -fsS https://anime-srez.up.railway.app/api/health
curl -sS -i https://anime-srez.up.railway.app/api/me
curl -sS -i https://anime-srez.up.railway.app/login
```

Expected smoke results:

- `/api/health` returns `{"ok": true}`.
- `/api/me` returns `401` without a session.
- `/login` returns HTML with the Google login page.
- Authenticated browser login succeeds and catalog pages load.

## Daily Content Sync Cron

Use a separate Railway Cron service or Function that calls the production web
endpoint and exits. Do not mount/write the production SQLite volume from a
second service. The production Function entrypoint is
`railway-functions/daily-sync.ts`.

When using a same-repo Railway service, keep the normal Railway start command
and set the cron service role instead of using the Function entrypoint:

```text
ANIME_SERVICE_ROLE=daily-sync
```

Cron variables:

```text
ANIME_PUBLIC_URL=https://anime-srez.up.railway.app
ANIME_SYNC_TOKEN=<same secret as web>
ANIME_SYNC_MODE=daily
```

If Railway Function/Cron service deployment is unavailable, enable the web
service scheduler instead:

```text
ANIME_INTERNAL_DAILY_SYNC=1
ANIME_DAILY_SYNC_UTC_HOUR=2
ANIME_DAILY_SYNC_UTC_MINUTE=0
```

Railway evaluates cron schedules in UTC. For 03:00 Portugal/Madeira summer
time, set:

```text
0 2 * * *
```

The web endpoint records run duration/status/stats in `content_update_runs`,
records user-visible changes in `content_update_events`, and writes a JSON
`content_sync` line to `/data/logs/server.log`.

## References

- Railway CLI: `https://docs.railway.com/cli`
- Railway config as code: `https://docs.railway.com/config-as-code/reference`
- Railway volumes: `https://docs.railway.com/volumes`
