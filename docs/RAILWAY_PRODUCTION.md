# Railway Production Release

`OPERATIONS.md` is the central source of truth for the release checklist,
verification, dev/prod boundaries, env vars, and troubleshooting. This file is
a Railway-specific quick reference.

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
```

Optional access controls may also be set:

```text
ANIME_AUTH_ALLOWED_EMAILS
ANIME_AUTH_ALLOWED_DOMAINS
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

Use the full checklist in `OPERATIONS.md`. The abbreviated Railway sequence is:

1. Verify the intended local state on dev:

```bash
.venv/bin/python -m py_compile server.py scrape_animego.py scrape_yummyanime.py sync_videos.py backfill_players.py prune_non_playable.py update_backup.py test_app.py scripts/check_repo_hygiene.py scripts/check_data_health.py scripts/smoke_dev_app.py
.venv/bin/python -m unittest -v test_app.py
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

3. Upload the current SQLite catalog/state to the Railway volume when the
   database should change:

```bash
railway volume files --volume web-volume upload db/animego.sqlite /animego.sqlite --overwrite --json
```

`db/animego.sqlite` is ignored by git. It is not included in `railway up`, so
database release is a separate explicit step from code release.

4. Deploy the current working tree to Railway production:

```bash
railway up --service web --environment production --detach --message "production release"
```

`railway up` uploads from the current directory and respects `.gitignore`.
Use it only when the current working tree is the intended production code.

5. Watch deployment state and logs:

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

Expected smoke results:

- `/api/health` returns `{"ok": true}`.
- `/api/me` returns `401` without a session.
- `/login` returns HTML with the Google login page.
- Authenticated browser login succeeds and catalog pages load.

## References

- Railway CLI: `https://docs.railway.com/cli`
- Railway config as code: `https://docs.railway.com/config-as-code/reference`
- Railway volumes: `https://docs.railway.com/volumes`
