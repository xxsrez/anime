# Operations Runbook

This is the central source of truth for running Anime Local in dev, touching
Railway production, and releasing code or database changes.

## Golden Rules

- Dev/test/scratch is `http://127.0.0.1:8765/`.
- Production is Railway `anime-local` / environment `production` / service
  `web` at `https://anime-srez.up.railway.app`.
- Local ports `8766` and `8776` are retired. Do not use them for Anime.
- Production is release-only. Do not deploy, upload data, restart, scrape, or
  test against prod unless the user explicitly asks for a production operation.
- Always run the app with `.venv/bin/python`, not system `python3`. System
  Python may not have `google-auth`, which breaks Google login with
  `google-auth dependencies are not installed`.
- `db/animego.sqlite` is mutable local state and ignored by git. It is not
  included in `railway up`.
- Do not upload the whole SQLite database for routine title or episode updates.
  Use `sync_videos.py --emit-migration`, keep generated catalog/player SQL in
  ignored `data/private-migrations/`, copy it to the Railway volume, and apply
  it through `scripts/db_migrate.py`.
- Production has its own content-sync cron and normally advances independently
  of dev. Different catalog counts or fresher production episodes are expected,
  not a defect. Do not overwrite production with the older dev database merely
  to make the environments match.
- Do not download the whole production SQLite database during normal releases.
  Use an explicit read-only snapshot only for a requested audit or concrete
  drift investigation; keep user state and scraped data out of Git.
- Configure exactly one scheduler for production: a Railway Cron/Function or
  `ANIME_INTERNAL_DAILY_SYNC`, never both. All writers share the SQLite
  operation lock; cron fails fast when another data operation owns it.
- GitHub must contain only license-clean project source, docs, tests, and
  schema/control migrations. Do not commit scraped catalog/player data patches.
- `origin/main` is the production code ledger. In steady state it must point to
  the exact commit used by the last successfully smoke-tested production
  deployment.
- Never deploy dirty or unpushed code, and never advance `main` with unreleased
  code. The normal order is: verify a clean pushed candidate, deploy that exact
  commit, smoke production, then immediately fast-forward `main` to it.
- Never overlap production releases. A release owns the production lane until
  its deployment, migrations, smoke checks, and `main` promotion finish or the
  release is explicitly aborted.

## Environment Map

| Environment | URL | Code | Database | Rule |
| --- | --- | --- | --- | --- |
| dev/test/scratch | `http://127.0.0.1:8765/` | `/Users/andrey/Projects/Home/Anime` | `db/animego.sqlite` | Safe for edits, restarts, scraping, indexing, and browser checks. |
| production | `https://anime-srez.up.railway.app` | Railway `anime-local` / `web` / `production` | Railway volume `web-volume` mounted at `/data`; app DB `/data/animego.sqlite` | Release-only after explicit user request. |

## Local Dev

Install/use the project virtualenv:

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
```

Required local `.env` values:

```text
GOOGLE_CLIENT_ID=...
ANIME_ADMIN_EMAIL=...
```

Optional local access controls:

```text
ANIME_AUTH_ALLOWED_EMAILS=...
ANIME_AUTH_ALLOWED_DOMAINS=...
```

Use a durable random signing secret in production or any multi-process setup so
an in-flight Google callback survives a restart or lands on another replica:

```text
ANIME_GOOGLE_AUTH_STATE_SECRET=...
```

When unset, the server uses a secure process-local fallback; outstanding login
states then expire on restart, while existing application sessions remain valid.

If `ANIME_AUTH_ALLOWED_EMAILS` is set, it must include `ANIME_ADMIN_EMAIL`.

Start dev in a durable tmux session:

```bash
tmux kill-session -t anime-dev 2>/dev/null || true
tmux new-session -d -s anime-dev 'cd /Users/andrey/Projects/Home/Anime && .venv/bin/python server.py --host 127.0.0.1 --port 8765 --db db/animego.sqlite'
```

Check it:

```bash
tmux list-sessions | rg '^anime-dev'
lsof -nP -iTCP:8765 -sTCP:LISTEN
curl -fsS http://127.0.0.1:8765/api/health
```

Open logs/live output:

```bash
tmux capture-pane -pt anime-dev -S -120
tail -120 data/logs/server.log
tail -120 data/logs/client-errors.log
```

Stop dev:

```bash
tmux kill-session -t anime-dev
```

## Dev Login Troubleshooting

If Google login fails:

1. Check the server process is using `.venv/bin/python`:

```bash
ps -p "$(lsof -tiTCP:8765 -sTCP:LISTEN)" -o pid,command
```

2. Check `google-auth` imports in the same runtime:

```bash
.venv/bin/python - <<'PY'
import google.auth
import google.oauth2.id_token
print("google-auth ok")
PY
```

3. Confirm auth config is present:

```bash
curl -fsS 'http://127.0.0.1:8765/api/auth/config?next=%2F' | python3 -m json.tool
```

4. A fake token should return `401 invalid Google credential`, not `503`:

```bash
curl -sS -i -X POST http://127.0.0.1:8765/api/auth/google \
  -H 'Content-Type: application/json' \
  --data '{"credential":"not-a-real-token","next":"/"}'
```

If it returns `503` with `google-auth dependencies are not installed`, restart
dev through `.venv/bin/python`.

## Verification Before Release

Run these before any release:

```bash
ruff check .
pip-audit -r requirements.txt
.venv/bin/python -m compileall -q server.py animego_scans.py content_updates.py scrape_animego.py \
  scrape_yummyanime.py sync_videos.py backfill_players.py prune_non_playable.py \
  update_backup.py scripts test_app.py test_animego_scans.py test_db_migrate.py \
  test_pipeline_hardening.py
.venv/bin/python -m unittest discover -v -p 'test*.py'
find static browser-extension/animego-scanner -name '*.js' -print0 | xargs -0 -n1 node --check
node static/animego_scan_ui.test.js
node --test static/frontend_runtime.test.js
npm test --prefix browser-extension/animego-scanner
npx --yes --package typescript@5.9.3 tsc railway-functions/daily-sync.ts \
  --noEmit --target ES2022 --module ES2022 --lib ES2022,DOM --strict
sh -n scripts/*.sh
.venv/bin/python scripts/check_repo_hygiene.py
.venv/bin/python scripts/smoke_dev_app.py
```

For database/catalog changes, also run:

```bash
.venv/bin/python scripts/check_data_health.py
```

After frontend or route changes, restart the already-running dev server. Static
files are read fresh, but Python route handlers are loaded only on process
start.

## Production Configuration

Railway target:

- Project: `anime-local`
- Environment: `production`
- Service: `web`
- Public URL: `https://anime-srez.up.railway.app`
- Volume: `web-volume`
- Volume mount: `/data`
- App database: `/data/animego.sqlite`
- Logs directory: `/data/logs`
- Start command: `sh scripts/railway_start.sh`

Required Railway variables for `web` in `production`:

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

Optional Railway variables:

```text
ANIME_AUTH_ALLOWED_EMAILS
ANIME_AUTH_ALLOWED_DOMAINS
```

The Google OAuth client must include this authorized JavaScript origin:

```text
https://anime-srez.up.railway.app
```

Do not paste raw `railway variable list --json` or `railway variable list --kv`
output into reports; those commands expose values.

## Production Release

Use only after the user explicitly asks for a production release.

1. Confirm Railway context:

```bash
railway status
railway volume list --json
```

2. Run verification from `## Verification Before Release`.

3. Pin the release to a clean pushed commit based on the current `origin/main`.
   Keep these variables in the same shell for the remaining release steps:

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

Any non-zero command aborts the release shell. Do not disable this fail-closed
mode or continue with later steps after a failed guard.

If the candidate is not a descendant of `origin/main`, reconcile it on dev and
rerun the full verification gate before touching production.

4. If production data should change, generate a migration from the intended
   local database state. Do not download production SQLite just to generate a
   routine patch:

```bash
.venv/bin/python sync_videos.py \
  --db db/animego.sqlite \
  --mode manual \
  --yummy-ref https://ru.yummyani.me/catalog/item/example \
  --emit-migration 2026-07-08_example-title \
  --stop-on-error
```

For recurring new episodes:

```bash
.venv/bin/python sync_videos.py \
  --db db/animego.sqlite \
  --mode hourly \
  --source yummyanime \
  --source animego \
  --emit-migration 2026-07-08_hourly-catalog-refresh \
  --stop-on-error
```

For a historical year backfill, generate SQL locally and deploy/apply it; do
not run the broad scraper on production:

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

Review the generated SQL locally, but do not commit it. It should stay under
ignored `data/private-migrations/`. Skip private-patch upload for code-only
releases. Apply the patch locally and verify dev before uploading it to
Railway. See `Incremental_DB_Update.md` for the full data-update workflow.

Full SQLite upload is emergency-only. Use it only for a deliberate full restore,
not for adding titles or episodes:

```bash
railway volume files -v web-volume upload db/animego.sqlite /animego.sqlite --overwrite --json
```

5. Deploy the exact candidate code tree:

```bash
test "$(git rev-parse HEAD)" = "$RELEASE_SHA"
test -z "$(git status --porcelain=v1)"
railway up --service web --environment production --detach \
  --message "production release $RELEASE_MARKER"
```

`railway up` uploads the current working tree and respects `.gitignore`. Use it
only after the checks above prove that the working tree is the exact committed
and pushed release candidate.

6. Wait for `SUCCESS` from the deployment whose message contains the exact
   release SHA. Do not apply migrations or smoke the URL while Railway may still
   be serving the previous deployment:

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

7. Copy private data patches to the Railway volume when production data should
   change:

```bash
railway volume files -v web-volume upload \
  data/private-migrations/2026-07-08_catalog-2020-2019 \
  /private-migrations/2026-07-08_catalog-2020-2019 \
  --overwrite \
  --json
```

Skip this step for code-only releases.

8. Apply tracked schema/control migrations on every release. Add the private
   migration root only when this release deliberately changes catalog data:

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

For a data release, add `--root /data/private-migrations`. A cron run that loses
the lock returns `423` and must fail rather than queue behind the release.

9. Inspect the successful deployment logs:

```bash
railway deployment list --json
railway logs --latest --lines 100
```

10. Smoke-check production:

```bash
curl -fsS https://anime-srez.up.railway.app/api/health
curl -sS -i https://anime-srez.up.railway.app/api/me
curl -sS -i https://anime-srez.up.railway.app/login
```

Expected:

- `/api/health` returns `{"ok": true}`.
- `/api/me` returns `401` without a session.
- `/login` returns the Google login HTML.
- Authenticated browser login succeeds.

11. After every successful production smoke, fast-forward `main` to the exact
    released commit and verify the remote ref. `main` must not move before this
    point during the normal release flow:

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

If deployment, migration/data-patch apply, or smoke fails, do not move `main`.
Repair and release a new candidate, or roll production back to the captured
`$MAIN_BEFORE_RELEASE` and smoke the rollback. If `origin/main` changed
concurrently after deployment, do not create a merge commit: reconcile on dev,
verify, and release a new fast-forward candidate.

## Recovering Main/Production Drift

Use this only when the invariant was already broken and production contains
code not represented by `main`. Outstanding branches alone are not a recovery
case; combine them on a candidate branch and use the normal production-first
flow. Drift recovery is the one temporary exception where `main` moves before
the production deployment:

1. Fetch and audit every local and remote branch. Include only intended release
   changes; do not merge stale or experimental branches merely because they
   exist.
2. Merge the intended branches into local `main`, preferring fast-forward
   merges where ancestry permits.
3. Run the complete dev gate and browser checks from the resulting `main`.
4. Push `main`, then prove that the clean local tree is exactly `origin/main`:

```bash
set -euo pipefail
git push origin main
git fetch origin --prune
test -z "$(git status --porcelain=v1)"
RELEASE_SHA="$(git rev-parse origin/main)"
test "$(git rev-parse HEAD)" = "$RELEASE_SHA"
RELEASE_MARKER="$RELEASE_SHA-$(.venv/bin/python -c 'import uuid; print(uuid.uuid4())')"
```

5. Deploy exactly `$RELEASE_SHA` with the SHA in the Railway deployment message
   using `$RELEASE_MARKER`; use the same matching-ID wait from normal release
   step 6 and require that deployment to remain the latest `SUCCESS`.
6. Apply migrations, smoke production, and verify both production and
   `origin/main` before declaring reconciliation complete.

Complete this recovery as one operation, then return to the normal
production-first, fast-forward-main-after-smoke procedure above.

## Admin Access

Admin access is controlled by exactly one environment-specific email:

```text
ANIME_ADMIN_EMAIL=...
```

The admin UI is `/admin`; admin data API is `/api/admin/users`. Non-admin users
must not see an admin link, and direct `/admin` or `/api/admin/users` requests
must not work for them.

## Daily Content Sync

Automatic title/episode discovery should run through the production web
service, not by mounting the SQLite volume into a separate writer. The web
service exposes a token-protected endpoint:

```text
POST /api/internal/daily-sync?mode=daily
Authorization: Bearer $ANIME_SYNC_TOKEN
```

The endpoint runs `sync_videos.py` with both sources by default, writes
`content_update_runs` and `content_update_events`, invalidates the catalog
cache, and logs a JSON `content_sync` entry to `server.log`.

AnimeGO currently rejects the Railway/cloud egress while remaining reachable
from the trusted local egress. Keep the direct web sync restricted to
YummyAnime instead of accepting a permanently partial cron:

```text
ANIME_CONTENT_SYNC_SOURCES=yummyanime
```

AnimeGO is collected separately by `scripts/animego_push_worker.py`. The worker
reads the protected production manifest, checks every exposed episode in daily
mode, and posts a versioned snapshot bundle back to the web service. The web
process validates freshness and complete coverage, takes the normal database
operation lock, writes SQLite/update events, and advances
`animego:<mode>:last_success` only after a complete successful run:

```bash
.venv/bin/python scripts/animego_push_worker.py --force
```

Authenticated catalog users can also run additive catch-up scans through the
unpacked Chrome extension. Keep that path separate from the trusted worker:
user jobs receive only a job-scoped token, never `ANIMEGO_PUSH_TOKEN`, and
do not advance the worker's last-success marker. Both paths still validate and
write on the web process under the shared database-operation lock. Setup,
Partial/Full selection, job expiry/resume, attribution audit, and troubleshooting
are documented in `docs/guides/animego-scanner/README.md`.

The push boundary uses its own high-entropy `ANIMEGO_PUSH_TOKEN`; do not reuse
the cron-trigger `ANIME_SYNC_TOKEN`:

```text
GET  /api/internal/animego-sync-manifest
POST /api/internal/animego-push-sync
Authorization: Bearer $ANIMEGO_PUSH_TOKEN
```

On the trusted macOS host, install the hourly catch-up worker once:

```bash
scripts/install_animego_push_launch_agent.sh
launchctl print gui/$(id -u)/com.xxsrez.animego-sync
tail -f ~/Library/Logs/Anime/animego-push-worker.log
```

It checks hourly but skips collection while the production AnimeGO success is
less than 20 hours old. Failures remain non-successful and are retried on the
next hourly invocation. The collection has a 30-minute cooperative deadline.
This worker requires the Mac to be running with network access and an
authenticated Railway CLI; move the same worker to an always-on allowed egress
if those conditions cannot be guaranteed.

Remove `ANIME_CONTENT_SYNC_SOURCES=yummyanime` only after direct AnimeGO
requests from the web container are healthy again. Supported direct-web values
are `yummyanime` and `animego`, separated by commas or spaces; at least one
source is required.

Create a separate Railway Cron service or Function that exits after calling the
endpoint. The production Function entrypoint is
`railway-functions/daily-sync.ts`. Enable exactly one scheduler path; do not
also enable the built-in scheduler on the web service.

For a same-repo Railway service, keep the normal Railway start command and
select the Python cron script through the service role:

```text
ANIME_SERVICE_ROLE=daily-sync
```

Required cron-service variables:

```text
ANIME_PUBLIC_URL=https://anime-srez.up.railway.app
ANIME_SYNC_TOKEN=<same secret as web>
ANIME_SYNC_MODE=daily
```

Optional:

```text
ANIME_SYNC_TIMEOUT_SECONDS=1800
ANIME_CRON_LOG_PATH=/tmp/anime-daily-sync.jsonl
```

If Railway Function/Cron service deployment is unavailable, disable that
external scheduler and let the web service run the same daily sync internally:

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

The cron script logs JSON start/finish/error lines to stdout and, if
`ANIME_CRON_LOG_PATH` is set, appends them to that file. Persistent production
run history lives in SQLite table `content_update_runs`; user-visible recent
changes live in `content_update_events`. Partial source results return `502` and
do not advance `last_success`; overlapping data operations return `423`. The
built-in scheduler catches up immediately after a missed daily slot, retries a
busy database after five minutes, and retries failures after thirty minutes.
