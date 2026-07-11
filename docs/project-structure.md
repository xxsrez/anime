# Project Structure

This page shows where the main repository files live. It describes the intended
layout; verify the live tree before making changes because ignored local data
can change outside git.

## Root Files

| Path | Purpose |
| --- | --- |
| `README.md` | Public project entrypoint and short scraper/app notes. |
| `requirements.txt` | Python runtime dependencies. |
| `requirements-dev.txt` | Pinned test, browser, lint, and security-audit tooling. |
| `pyproject.toml` | Ruff configuration and Python target. |
| `.python-version` | CI/local Python version contract. |
| `railway.json` | Railway build/deploy configuration. |
| `.env.example` | Local environment variable template. |
| `.gitignore` | Keeps local databases, logs, caches, env files, and venvs out of git. |

## Application Code

| Path | Purpose |
| --- | --- |
| `server.py` | HTTP server, Google auth, API, canonical catalog, recommendations, logging. |
| `animego_scans.py` | User-powered AnimeGo scan selection, job lifecycle, validation, additive imports, attribution, and extension ZIP packaging. |
| `scrape_animego.py` | Base SQLite schema and AnimeGO scraper/importer. |
| `scrape_yummyanime.py` | YummyAnime/YummyAni importer and provider parsing. |
| `sync_videos.py` | Video-first periodic updater for hourly, daily, and full sync modes. |
| `backfill_players.py` | Backfills player/video rows for source titles missing playable coverage. |
| `prune_non_playable.py` | Removes source rows that still have no playable embed URL. |
| `update_backup.py` | Updates the ignored local recovery snapshot. |
| `test_app.py` | Regression tests for server, scraper, sync, auth, catalog, and recommendations behavior. |
| `test_animego_scans.py` | User-scan selection, additive writes, attribution, lifecycle, ZIP, and HTTP-route regressions. |
| `test_db_migrate.py` | Migration planning, drift, preflight, atomicity, and streaming-diff tests. |
| `test_pipeline_hardening.py` | Locking, URL safety, backup, cron, and recovery regressions. |

## Frontend

| Path | Purpose |
| --- | --- |
| `static/index.html` | Authenticated app shell. |
| `static/app.js` | Catalog UI state, filtering, sorting, detail view, player controls, user state, recommendations. |
| `static/frontend_runtime.js` | Independently testable URL, date, watch-evidence, and keyed-queue primitives. |
| `static/frontend_runtime.test.js` | Node regression tests for those browser-independent primitives. |
| `static/app.css` | Compact dark UI styling. |
| `static/scanner-setup.html`, `static/scanner-setup.js`, `static/scanner-setup.css` | Authenticated one-time Chrome extension setup page. |
| `static/login.html` | Google login page. |
| `static/login.js` | Google Identity Services login flow and session handoff. |
| `static/admin.html` | Admin page shell. |
| `static/admin.js` | Admin users/activity UI. |
| `static/client_errors.js` | Browser error reporting helper. |
| `static/favicon.svg` | App icon. |

## Browser Extension

| Path | Purpose |
| --- | --- |
| `browser-extension/animego-scanner/` | Unpacked Chrome Manifest V3 extension plus Node parser/state tests; it receives assigned jobs, reads AnimeGo player metadata through the user's browser, checkpoints progress, and posts validated results. |

## Scripts

| Path | Purpose |
| --- | --- |
| `scripts/check_repo_hygiene.py` | Fails when local mutable files, logs, caches, or large artifacts are tracked or present in git history. |
| `scripts/check_data_health.py` | Validates local SQLite integrity, backup checksums, and the watchable-catalog invariant. |
| `scripts/smoke_dev_app.py` | Starts a temporary local server and checks login guards, catalog, and recommendations. |
| `scripts/operation_lock.py` | Canonical bounded cross-process lock shared by DB writers and snapshots. |
| `scripts/atomic_publish.py` | Atomic staged-directory publication with rollback. |
| `scripts/http_safety.py` | HTTPS/host/redirect/address validation for scraper and cron HTTP clients. |
| `scripts/db_migrate.py` | Ordered, preflighted, batch-atomic SQLite migration runner. |
| `scripts/db_data_diff.py` | Streaming catalog-data migration generator. |
| `scripts/missing_db_bootstrap.py` | Railway 503 bootstrap that hands off after a stable valid DB appears. |
| `scripts/railway_start.sh` | Railway start command wrapper for `server.py`. |
| `scripts/animego_push_worker.py` | Collects AnimeGO through an allowed egress and sends a validated bundle to the protected production endpoint. |
| `scripts/install_animego_push_launch_agent.sh` | Installs the hourly macOS catch-up worker for AnimeGO. |
| `scripts/run_animego_push_worker.sh` | LaunchAgent wrapper with a stable PATH and local logging. |

## Automation And Migrations

| Path | Purpose |
| --- | --- |
| `.github/workflows/ci.yml` | Clean-checkout lint, audit, compile, Python/Node/TypeScript tests, and hygiene gate. |
| `.github/dependabot.yml` | Weekly Python and GitHub Actions dependency updates. |
| `migrations/` | License-clean schema, index, cache-revision, and integrity-repair migrations. |
| `railway-functions/daily-sync.ts` | Fail-closed Railway cron client for the protected web sync endpoint. |

## Documentation

| Path | Purpose |
| --- | --- |
| `docs/README.md` | Documentation index and reading order. |
| `docs/overview.md` | Product summary, current workflows, data sources, playback, recommendations, limitations. |
| `docs/architecture.md` | Runtime stack, main components, API, data flow, scraper notes, verification. |
| `docs/project-structure.md` | This file. |
| `docs/instructions/` | Working rules, environment rules, and operations runbook. |
| `docs/guides/` | Component and deployment guides. |
| `docs/design/` | Reserved for historical/original specifications. |
| `docs/tasks/` | Reserved for complex investigations with changelogs. |
| `docs/reports/` | Reserved for final analysis reports. |

## Ignored Local State

These paths are part of the local workflow but are intentionally not committed:

| Path | Purpose |
| --- | --- |
| `db/animego.sqlite` | Mutable local app database. |
| `db/backups/current/` | Local recovery snapshot for database and user state exports. |
| `data/logs/` | Server and browser error logs. |
| `.env` | Local secrets/config values. |
| `.venv/` | Local Python virtual environment. |
| `__pycache__/`, `scripts/__pycache__/` | Python bytecode caches. |

Do not move ignored mutable data into tracked documentation. Summaries belong
in docs; databases, logs, raw secrets, and generated caches stay ignored.
