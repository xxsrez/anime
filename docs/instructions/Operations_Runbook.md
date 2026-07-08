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
- Do not download the whole production SQLite database during normal releases.
  Local and production catalog/player state should stay synchronized because the
  same private patches are applied locally first and then on production. Full DB
  download is for explicit spot audits or concrete drift evidence.
- GitHub must contain only license-clean project source, docs, tests, and
  schema/control migrations. Do not commit scraped catalog/player data patches.

## Environment Map

| Environment | URL | Code | Database | Rule |
| --- | --- | --- | --- | --- |
| dev/test/scratch | `http://127.0.0.1:8765/` | `/Users/andrey/Projects/Home/Anime` | `db/animego.sqlite` | Safe for edits, restarts, scraping, indexing, and browser checks. |
| production | `https://anime-srez.up.railway.app` | Railway `anime-local` / `web` / `production` | Railway volume `web-volume` mounted at `/data`; app DB `/data/animego.sqlite` | Release-only after explicit user request. |

## Local Dev

Install/use the project virtualenv:

```bash
.venv/bin/python -m pip install -r requirements.txt
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
.venv/bin/python -m py_compile server.py scrape_animego.py scrape_yummyanime.py sync_videos.py backfill_players.py prune_non_playable.py update_backup.py test_app.py scripts/check_repo_hygiene.py scripts/check_data_health.py scripts/smoke_dev_app.py scripts/db_migrate.py scripts/db_data_diff.py test_db_migrate.py
.venv/bin/python -m unittest -v test_app.py test_db_migrate.py
node --check static/app.js
node --check static/login.js
node --check static/admin.js
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
ANIME_SESSION_SECURE=1
ANIMEGO_DB=/data/animego.sqlite
ANIME_LOG_DIR=/data/logs
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

3. If production data should change, generate a migration from the intended
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

4. Deploy code and tracked schema/control migrations:

```bash
railway up --service web --environment production --detach --message "production release"
```

`railway up` uploads the current working tree and respects `.gitignore`. Use it
only when the working tree is exactly the intended production code.

5. Copy private data patches to the Railway volume when production data should
   change:

```bash
railway volume files -v web-volume upload \
  data/private-migrations/2026-07-08_catalog-2020-2019 \
  /private-migrations/2026-07-08_catalog-2020-2019 \
  --overwrite \
  --json
```

Skip this step for code-only releases.

6. Apply pending migrations when production data should change:

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

Skip this step for code-only releases.

7. Watch deployment:

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

Expected:

- `/api/health` returns `{"ok": true}`.
- `/api/me` returns `401` without a session.
- `/login` returns the Google login HTML.
- Authenticated browser login succeeds.

## Admin Access

Admin access is controlled by exactly one environment-specific email:

```text
ANIME_ADMIN_EMAIL=...
```

The admin UI is `/admin`; admin data API is `/api/admin/users`. Non-admin users
must not see an admin link, and direct `/admin` or `/api/admin/users` requests
must not work for them.
