# Environments And Release Rules

`OPERATIONS.md` is the central source of truth for dev process management,
Railway production, releases, env vars, smoke checks, and troubleshooting. This
file is a short environment map only.

This project uses local development plus Railway production. Keep the split
strict so development work cannot break the production service.

## Environment Map

| Environment | URL | Files | Database | Agent rule |
| --- | --- | --- | --- | --- |
| dev/test/scratch | `http://127.0.0.1:8765/` | `/Users/andrey/Projects/Home/Anime` | `/Users/andrey/Projects/Home/Anime/db/animego.sqlite` | Safe for regular edits, scraping, indexing, restarts, and browser testing. |
| production | `https://anime-srez.up.railway.app` | Railway project `anime-local`, service `web`, environment `production` | Railway volume `web-volume` mounted at `/data`, app DB `/data/animego.sqlite` | Release-only. Deploy only after an explicit production release request. |

Local port `8766` and port `8776` are retired for this project. Do not use them
for Anime dev/prod work unless the user explicitly changes the scheme again.

## Development

Run and test the current working tree on dev. For durable dev server commands,
use `OPERATIONS.md`.

```bash
cp .env.example .env
printf 'GOOGLE_CLIENT_ID=%s\n' '...apps.googleusercontent.com' > .env
.venv/bin/python server.py --host 127.0.0.1 --port 8765 --db db/animego.sqlite
```

The Google OAuth client must be type `Web application`. Add the exact dev URL
origin you open in the browser to Authorized JavaScript origins, usually
`http://127.0.0.1:8765` and optionally `http://localhost:8765`. The normal
login button uses Google Identity Services popup callbacks, so Authorized
redirect URIs are not needed for the standard local login path.
Set `ANIME_ADMIN_EMAIL` in the environment that should expose `/admin`. Admin
access is checked server-side and is intentionally separate from
`ANIME_AUTH_ALLOWED_EMAILS`; when the email allowlist is enabled, include the
admin email in both variables.

Use dev for:

- Code and UI changes.
- Scraper/indexing experiments.
- Database refreshes and backup updates.
- Performance checks.
- Browser smoke tests.

If `db/animego.sqlite` or `db/backups/current/animego.sqlite` changes, treat
that as local mutable catalog state and verify the intended database snapshot.
These files are ignored by git and should not be committed.

## Production

Production runs on Railway. Use the central runbook before touching prod:

- `docs/OPERATIONS.md`

Railway-specific details are also summarized in:

- `docs/RAILWAY_PRODUCTION.md`

Useful read-only status commands:

```bash
railway status
railway deployment list --json
railway logs --latest --lines 100
```

Code release command, only after the user explicitly asks to release and after
the `OPERATIONS.md` release checklist passes:

```bash
railway up --service web --environment production --detach --message "production release"
```

Database release command, only when the production database should change:

```bash
railway volume files --volume web-volume upload db/animego.sqlite /animego.sqlite --overwrite --json
```

The SQLite catalog and local backup snapshots are not part of git or
`railway up`. Treat production database refreshes as a separate explicit
operation from code release.

## Normal Workflow

1. Make changes and run all experiments on `http://127.0.0.1:8765/`.
2. Verify the app on dev, including browser checks for UI/player changes.
3. Commit the intended code and docs only; keep database snapshots under ignored
   `db/`.
4. Wait for an explicit user release command.
5. Upload `db/animego.sqlite` to the Railway volume if the database should
   change.
6. Deploy the intended code to Railway.
7. Smoke-check production after release, then stop making changes there.

Never use Railway production as scratch space. If a change needs investigation,
reproduce it on dev first.
