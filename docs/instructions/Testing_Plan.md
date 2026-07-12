# Testing Plan

This plan is the shared checklist for agents that verify Anime Local before and
after releases. Test each target separately: a pass on one target does not prove
that another target is healthy.

## Site Versions And Test Targets

### 1. Local dev working tree

- URL: `http://127.0.0.1:8765/`
- Code: current files in `/Users/andrey/Projects/Home/Anime`
- Database: ignored local `db/animego.sqlite`
- Purpose: normal development, browser testing, UI fixes, scraper/indexing
  experiments, and automated smoke checks.

Treat this as the first proof point for every change. It is allowed to restart,
edit, scrape, and mutate local state here.

### 2. Temporary automated test server

- URL: usually a random or high local port, for example `18765`
- Code: current working tree
- Database: temporary copy of `db/animego.sqlite`
- Purpose: agent-owned browser automation that must not pollute the real local
  database, user sessions, favorites, or progress.

Use this target for Playwright/mobile layout checks, authenticated fake-session
tests, destructive state toggles, and repeatable agent sweeps.

### 3. Railway production

- URL: `https://anime-srez.up.railway.app`
- Code: latest deployed Railway service bundle
- Database: Railway volume `web-volume`, app DB `/data/animego.sqlite`
- Purpose: real user-facing site after an explicit production release.

Production is release-only. Do not use it as scratch space. Do not upload a
database, deploy code, restart services, or run scraping against production
unless the user explicitly requested that operation.

### 4. Production database snapshot

The code release and database release are separate. `railway up` does not upload
`db/animego.sqlite`. If the task changes only CSS/JS/Python code, test and
release code only. If the catalog/player data should change, test the local DB
snapshot first and upload it to the Railway volume only as a separate explicit
release step.

## Release Gate

Before any production release, run the verification set from
`Operations_Runbook.md`:

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

The unit/integration suite builds temporary SQLite fixtures and must pass from a
clean checkout without `db/animego.sqlite`. The mutable dev database is a
separate operational target, not a hidden test dependency.

## Browser Matrix

### Desktop

- Chrome desktop, authenticated user.
- At least one anonymous window or cleared-session context.
- Widths around `1280x800` and `1440x900`.

### Mobile

Target modern iPhones, not legacy tiny devices:

- `390x844` - iPhone 12/13/14 compact CSS viewport.
- `393x852` - iPhone 14/15 Pro class.
- `430x932` - iPhone Pro Max class.
- `390x620` - stress case for visible browser chrome and reduced viewport
  height.

For mobile UI changes, verify the real browser behavior, not only CSS review.
The app must remain usable when Safari/Chrome browser bars reduce the visible
viewport.

## Core Functional Checks

Run these on local dev first. Repeat the read-only subset on production after a
release.

- Anonymous app route redirects to `/login`.
- Login page renders Google One Tap/button fallback and does not show a blank
  screen.
- Authenticated app loads the account row, catalog count, search, sort, filters,
  tabs, and initial title detail.
- Logout clears the session and returns to login.
- Catalog search filters titles by Russian/English titles, subtitles, sources,
  genres, and metadata.
- Sort direction and sort field update the visible order.
- Add/remove filters for genre, year, type, status, source, and video
  availability.
- View tabs work independently: `Все`, `Избр.`, `Смотрю`, `Советы`.
- Recommendation tab shows ranked items, reasons, and profile metadata.
- Selecting a catalog title loads detail and updates the URL.
- Shared slug links restore title, episode, source, translation, and provider
  when those params are valid.
- Favorite persists independently of the mutually exclusive `Не смотрю`,
  `Смотрю`, and `Просмотрено` statuses; all six combinations persist per Google
  user and do not leak to another user.
- The title page does not expose `not_interested` as a fourth status or action.
- Episode selector and previous/next episode controls update player source
  state.
- A Kodik serial player's internal episode selector updates the outer episode,
  source, shared URL, and last-opened cursor without reloading the iframe.
- Provider autostart alone does not create watch progress; a real focus/click,
  fullscreen/PiP, or an already-engaged session is still required.
- Source, translation, and provider selectors choose a playable embed URL.
- Fullscreen button targets the iframe wrapper and exits cleanly.
- PiP button either opens document Picture-in-Picture where supported or shows
  the expected fallback message.
- Admin link appears only for the configured admin user.
- `/admin` and `/api/admin/users` are inaccessible to non-admin users.

## Mobile Layout Checks

For each modern iPhone viewport:

- The first screen shows the brand, account row, search, sorting/filter controls,
  tabs, and at least one reachable catalog card.
- The document/body can scroll; the app must not be trapped inside a clipped
  `100vh` or `100dvh` shell.
- The catalog list has enough height to browse multiple cards.
- Tapping a catalog card moves the user to the detail area on mobile.
- Detail title, poster, compact favorite/status controls, genres, fields,
  episode picker, player, and source panel are reachable by normal vertical
  scrolling.
- Long Russian titles, subtitles, metadata, and filter labels do not overflow
  controls.
- The lower safe area does not cover the final controls on iPhone.
- Browser back/forward keeps the selected title/link state understandable.

## Production Smoke After Release

After `railway up`, run:

```bash
curl -fsS https://anime-srez.up.railway.app/api/health
curl -sS -i https://anime-srez.up.railway.app/api/me
curl -sS -i https://anime-srez.up.railway.app/login
```

Expected:

- `/api/health` returns `{"ok": true}`.
- `/api/me` returns `401` without a session.
- `/login` returns the Google login HTML.

Then run one authenticated browser smoke on production:

- Login succeeds.
- Catalog loads.
- Mobile viewport can browse and open a title.
- One playable title opens its player wrapper.
- Logout succeeds.

## Agent Handoff Rules

- Assign agents to one target at a time: dev, temporary server, or production.
- Require agents to report the exact URL, viewport, auth state, and database
  target they tested.
- Require screenshots for mobile layout failures and production UI failures.
- Treat production failures as release blockers or rollback candidates, not as
  reasons to experiment directly on production.
- If an agent changes code or data while testing, rerun the release gate before
  any deploy.
