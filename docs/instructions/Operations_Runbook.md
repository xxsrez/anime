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
  Use `Incremental_DB_Update.md` and `scripts/prod_incremental_update.py`.

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
.venv/bin/python -m py_compile server.py scrape_animego.py scrape_yummyanime.py sync_videos.py backfill_players.py prune_non_playable.py update_backup.py test_app.py scripts/check_repo_hygiene.py scripts/check_data_health.py scripts/smoke_dev_app.py
.venv/bin/python -m unittest -v test_app.py
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

3. If production data should change, prefer an incremental in-place update
   inside Railway:

```bash
.venv/bin/python scripts/prod_incremental_update.py \
  --yummy-ref https://ru.yummyani.me/catalog/item/example \
  --stop-on-error
```

For recurring new episodes:

```bash
.venv/bin/python scripts/prod_incremental_update.py \
  --mode hourly \
  --source yummyanime \
  --source animego \
  --stop-on-error
```

Skip this step for code-only releases. See `Incremental_DB_Update.md` for the
full data-update workflow, remote backup policy, and AI-agent checklist.

Full SQLite upload is emergency-only. Use it only for a deliberate full restore,
not for adding titles or episodes:

```bash
railway volume files -v web-volume upload db/animego.sqlite /animego.sqlite --overwrite --json
```

4. Deploy code:

```bash
railway up --service web --environment production --detach --message "production release"
```

`railway up` uploads the current working tree and respects `.gitignore`. Use it
only when the working tree is exactly the intended production code.

5. Watch deployment:

```bash
railway deployment list --json
railway logs --latest --lines 100
```

6. Smoke-check production:

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
