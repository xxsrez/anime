# Anime Local Documentation

This directory is the durable project documentation for the local anime catalog
and player prototype.

## Reading order

1. `OVERVIEW.md` - product summary, current scope, user workflows, limitations.
2. `OPERATIONS.md` - central runbook for dev, prod, release, tmux, env vars,
   login troubleshooting, and smoke checks.
3. `ENVIRONMENTS.md` - short environment map; defer to `OPERATIONS.md` for
   commands.
4. `RAILWAY_PRODUCTION.md` - Railway-specific quick reference; defer to
   `OPERATIONS.md` for the release checklist.
5. `TECHNICAL.md` - runtime, data flow, API, scraper behavior, verification.
6. `DATA_MODEL.md` - SQLite tables, relationships, ID conventions, mutable data.
7. `CODE_STYLE.md` - project coding rules and contribution conventions.

## Quick start

Run the dev/test site:

```bash
cp .env.example .env
printf 'GOOGLE_CLIENT_ID=%s\n' '...apps.googleusercontent.com' > .env
.venv/bin/python server.py --port 8765
```

Open `http://127.0.0.1:8765/`.

Production is Railway service `web` at
`https://anime-srez.up.railway.app` and must only be updated after an explicit
release request. See `OPERATIONS.md` before touching dev process management,
Railway variables, production deploys, or database uploads.

Run the current verification set:

```bash
python3 -m py_compile server.py scrape_animego.py scrape_yummyanime.py sync_videos.py backfill_players.py prune_non_playable.py update_backup.py test_app.py scripts/check_repo_hygiene.py scripts/check_data_health.py scripts/smoke_dev_app.py
python3 scripts/check_repo_hygiene.py
python3 -m unittest -v test_app.py
node --check static/app.js
node --check static/login.js
node --check static/admin.js
```

For local database changes, also run:

```bash
python3 scripts/check_data_health.py
python3 scripts/smoke_dev_app.py
```

After frontend or API changes, restart any already-running `server.py` process.
The static files are read fresh, but Python route handlers are not reloaded in a
long-running process.

## Important boundaries

- The project stores catalog/player metadata in local SQLite.
- The browser app requires Google Sign-In; API/catalog/player routes require a
  valid local session cookie.
- Dev/test/scratch is local `8765`; production is Railway; local ports `8766`
  and `8776` are retired.
- The recommendation list is computed locally from current-user
  `user_title_state` and catalog metadata.
- The project does not download or host anime video streams.
- Playback uses third-party embed URLs saved in SQLite. The main catalog should
  not contain titles that have no playable `embed_url`.
- `db/animego.sqlite` is local mutable state and can change after scraping or
  user progress updates.
- Scraping depends on upstream site structure and can break when those sites
  change.
