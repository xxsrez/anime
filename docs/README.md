# Anime Local Documentation

This directory is the durable project documentation for the local anime catalog
and player prototype. It follows the `project-docs` layout:

- root pages explain the project, architecture, and repository structure;
- `instructions/` contains working rules and operational process;
- `guides/` contains current component and deployment how-tos;
- `design/`, `tasks/`, and `reports/` are reserved for future historical
  specifications, multi-day investigations, and final analyses.

## Reading order

1. `overview.md` - product summary, current scope, workflows, limitations.
2. `architecture.md` - runtime, data flow, API, scraper behavior,
   recommendations, and verification.
3. `project-structure.md` - where source, docs, ignored data, and generated
   artifacts live.
4. `instructions/Operations_Runbook.md` - central runbook for dev, prod,
   release, tmux, env vars, login troubleshooting, and smoke checks.
5. `instructions/Environment_Rules.md` - short dev/prod environment map.
6. `instructions/Testing_Plan.md` - shared browser, mobile, dev/prod, and
   release testing checklist for agents.
7. `guides/deployment/railway-production.md` - Railway-specific quick
   reference; defer to the operations runbook for the release checklist.
8. `guides/data-model/README.md` - SQLite tables, relationships, ID
   conventions, and mutable data.
9. `instructions/Code_Style_Instructions.md` - coding rules and contribution
   conventions.

## Directory Map

| Path | Purpose |
| --- | --- |
| `overview.md` | What Anime Local is and why it exists. |
| `architecture.md` | How the app, scrapers, auth, API, and UI work. |
| `project-structure.md` | Where files live and which paths are generated. |
| `instructions/` | Rules for working on this repo. |
| `guides/` | Current how-tos for components and deployment. |
| `design/` | Future original/historical specifications. |
| `tasks/` | Future complex investigations with changelogs. |
| `reports/` | Future final analysis documents. |

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
release request. See `instructions/Operations_Runbook.md` before touching dev
process management, Railway variables, production deploys, or database uploads.

Run the current verification set:

```bash
.venv/bin/python -m py_compile server.py scrape_animego.py scrape_yummyanime.py sync_videos.py backfill_players.py prune_non_playable.py update_backup.py test_app.py scripts/check_repo_hygiene.py scripts/check_data_health.py scripts/smoke_dev_app.py
.venv/bin/python scripts/check_repo_hygiene.py
.venv/bin/python -m unittest -v test_app.py
node --check static/app.js
node --check static/login.js
node --check static/admin.js
```

For local database changes, also run:

```bash
.venv/bin/python scripts/check_data_health.py
.venv/bin/python scripts/smoke_dev_app.py
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
