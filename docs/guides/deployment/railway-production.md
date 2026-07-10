# Railway Production Release

`../../instructions/Operations_Runbook.md` is the central source of truth for
the release checklist, verification, dev/prod boundaries, env vars, and
troubleshooting. This file is a Railway-specific quick reference.

Railway is the production environment for Anime Local. The retired localhost
production install at `127.0.0.1:8766` must not be used for releases.

`origin/main` is the ledger of the last successfully verified production
commit. Normal releases deploy a clean pushed candidate first and fast-forward
`main` to that exact SHA only after production smoke checks pass. See
`../../instructions/Operations_Runbook.md` for the drift-recovery exception.

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
ANIME_GOOGLE_AUTH_STATE_SECRET
ANIME_SESSION_SECURE=1
ANIMEGO_DB=/data/animego.sqlite
ANIME_LOG_DIR=/data/logs
ANIME_SYNC_TOKEN
ANIMEGO_PUSH_TOKEN
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
ruff check .
pip-audit -r requirements.txt
.venv/bin/python -m unittest discover -v -p 'test*.py'
find static -name '*.js' -print0 | xargs -0 -n1 node --check
node --test static/frontend_runtime.test.js
.venv/bin/python scripts/smoke_dev_app.py
```

Pin the candidate after verification and keep these values in the release
shell:

```bash
set -euo pipefail
git fetch origin --prune
test -z "$(git status --porcelain=v1)"
test "$(git branch --show-current)" != "main"
test "$(git rev-parse --abbrev-ref '@{u}')" != "origin/main"
RELEASE_SHA="$(git rev-parse HEAD)"
MAIN_BEFORE_RELEASE="$(git rev-parse origin/main)"
test "$RELEASE_SHA" = "$(git rev-parse '@{u}')"
git merge-base --is-ancestor "$MAIN_BEFORE_RELEASE" "$RELEASE_SHA"
RELEASE_MARKER="$RELEASE_SHA-$(.venv/bin/python -c 'import uuid; print(uuid.uuid4())')"
```

Keep fail-closed mode enabled for the entire release. Any failed guard,
deployment, migration, data patch, or smoke check aborts promotion.

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
releases. Production has its own content-sync cron and is expected to be ahead
of dev; different counts are not drift by themselves. Full database transfers
are for explicit spot audits, concrete invariant failures, or disaster
recovery. Never replace production with the older dev database to make counts
match.

Full upload is emergency-only for deliberate full restores:

```bash
railway volume files -v web-volume upload db/animego.sqlite /animego.sqlite --overwrite --json
```

4. Deploy the exact clean pushed candidate to Railway production:

```bash
test "$(git rev-parse HEAD)" = "$RELEASE_SHA"
test -z "$(git status --porcelain=v1)"
railway up --service web --environment production --detach \
  --message "production release $RELEASE_MARKER"
```

`railway up` uploads from the current directory and respects `.gitignore`.
Use it only when the checks above prove the tree matches the release commit.

5. Wait for the matching deployment to reach `SUCCESS`; do not let migrations
   or smoke checks accidentally target the previous deployment:

```bash
for attempt in $(seq 1 120); do
  DEPLOYMENT_JSON="$(
    railway deployment list --service web --environment production \
      --limit 20 --json |
      jq -c --arg message "production release $RELEASE_MARKER" \
        'map(select(.meta.cliMessage == $message)) | sort_by(.createdAt) | last // {}'
  )"
  RELEASE_DEPLOYMENT_ID="$(jq -r '.id // empty' <<<"$DEPLOYMENT_JSON")"
  DEPLOY_STATUS="$(jq -r '.status // empty' <<<"$DEPLOYMENT_JSON")"
  case "$DEPLOY_STATUS" in
    SUCCESS) break ;;
    FAILED|CRASHED|REMOVED|CANCELED|CANCELLED) exit 1 ;;
  esac
  sleep 5
done
test "$DEPLOY_STATUS" = "SUCCESS"
test -n "$RELEASE_DEPLOYMENT_ID"
LATEST_DEPLOYMENT_ID="$(
  railway deployment list --service web --environment production \
    --limit 20 --json | jq -r 'sort_by(.createdAt) | last | .id // empty'
)"
test "$LATEST_DEPLOYMENT_ID" = "$RELEASE_DEPLOYMENT_ID"
```

6. Copy private data patches to the Railway volume if production data should
   change:

```bash
railway volume files -v web-volume upload \
  data/private-migrations/2026-07-08_catalog-2020-2019 \
  /private-migrations/2026-07-08_catalog-2020-2019 \
  --overwrite \
  --json
```

7. Apply tracked schema/control migrations on every release. Include the
   private root only for a deliberate data patch:

```bash
railway ssh --service web --environment production '
  db="${ANIMEGO_DB:-/data/animego.sqlite}"
  python3 scripts/db_migrate.py apply \
    --db "$db" \
    --root migrations \
    --no-backup \
    --wait-lock \
    --lock-timeout 1800
'
```

For a data release, add `--root /data/private-migrations`. The migration runner
and production cron use the same lock; an overlapping cron invocation fails
fast rather than writing concurrently.

8. Inspect deployment state and logs:

```bash
railway deployment list --json
railway logs --latest --lines 100
```

9. Smoke-check production:

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

10. Immediately after successful smoke checks, promote the exact released SHA
   to `main` with no merge commit:

```bash
DEPLOYMENT_SNAPSHOT="$(
  railway deployment list --service web --environment production \
    --limit 20 --json
)"
test "$(
  jq -r --arg message "production release $RELEASE_MARKER" \
    'map(select(.meta.cliMessage == $message)) | sort_by(.createdAt) | last | .id // empty' \
    <<<"$DEPLOYMENT_SNAPSHOT"
)" = "$RELEASE_DEPLOYMENT_ID"
test "$(
  jq -r 'sort_by(.createdAt) | last | .id // empty' <<<"$DEPLOYMENT_SNAPSHOT"
)" = "$RELEASE_DEPLOYMENT_ID"
test "$(
  jq -r --arg id "$RELEASE_DEPLOYMENT_ID" \
    'map(select(.id == $id)) | last | .status // empty' <<<"$DEPLOYMENT_SNAPSHOT"
)" = "SUCCESS"
git fetch origin --prune
test "$(git rev-parse origin/main)" = "$MAIN_BEFORE_RELEASE"
git switch main
git pull --ff-only origin main
git merge --ff-only "$RELEASE_SHA"
git push origin main
git fetch origin --prune
test "$(git rev-parse origin/main)" = "$RELEASE_SHA"
```

Do not move `main` after a failed release. If fast-forward promotion is no
longer possible, reconcile, verify, and deploy a new candidate. For the
one-time recovery procedure when `main` and production already disagree, use
`Recovering Main/Production Drift` in the central runbook.

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

The web service directly syncs YummyAnime. AnimeGO currently blocks the
Railway/cloud egress, so keep `ANIME_CONTENT_SYNC_SOURCES=yummyanime` and run
`scripts/animego_push_worker.py` from a trusted allowed egress. The worker uses
the separate `ANIMEGO_PUSH_TOKEN` and sends validated data to the protected web
endpoint; only the web process writes production SQLite. See `Daily Content
Sync` in the central runbook for the worker and macOS LaunchAgent commands.
Remove the override only after direct AnimeGO requests from the web container
recover.

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
