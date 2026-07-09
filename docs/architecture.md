# Architecture

## Runtime Stack

- Python standard library HTTP server.
- SQLite for catalog, episodes, player metadata, auth sessions, and per-user
  local state.
- Google Identity Services ID tokens, verified server-side with `google-auth`.
- `beautifulsoup4` and `lxml` for scraping/parsing.
- Plain HTML, CSS, and JavaScript for the frontend.
- No frontend build step.
- No Python web framework.

## Main Components

- `server.py` serves static assets and JSON API endpoints from SQLite.
- `scrape_animego.py` owns the base SQLite schema and the AnimeGO scraper.
- `scrape_yummyanime.py` imports selected YummyAnime/YummyAni titles into the
  same schema.
- `sync_videos.py` is the periodic video-first updater for hourly/daily/full
  runs. It skips metadata-only rows by default and only rewrites known video
  sources when `--refresh-known` is passed.
- `static/index.html` defines the local app shell.
- `static/app.js` owns client state, rendering, filtering, sorting, player
  source selection, recommendations, player controls, and user-state PATCH
  calls.
- `static/app.css` owns the compact dark UI.
- `test_app.py` contains the current regression tests.

## Local Server

Run the dev/test server:

```bash
.venv/bin/python server.py --host 127.0.0.1 --port 8765
```

The default database is `db/animego.sqlite`. Override it with:

```bash
.venv/bin/python server.py --db /path/to/animego.sqlite
```

or with the `ANIMEGO_DB` environment variable for internal helper calls.

Authentication uses Sign in with Google. The login page calls Google One Tap
first and keeps the Google button as a fallback. Both paths use the popup
callback ID-token flow, then POST the credential to the local server and finish
through a top-level completion page that sets the session cookie. Google
requires a public OAuth client ID:

```bash
cp .env.example .env
printf 'GOOGLE_CLIENT_ID=%s\n' '...apps.googleusercontent.com' > .env
```

`server.py` loads `.env` by default. Exported environment variables still win
over values from `.env`.

Optional restrictions:

```bash
export ANIME_AUTH_ALLOWED_EMAILS="one@example.com,two@example.com"
export ANIME_AUTH_ALLOWED_DOMAINS="example.com"
```

Admin access is separate from the general login allowlist:

```bash
export ANIME_ADMIN_EMAIL="one@example.com"
```

Only that one Google email can open `/admin` or `/api/admin/users`. If
`ANIME_AUTH_ALLOWED_EMAILS` is set, include the same email there so the admin
account can pass the normal login gate.

Sessions use an opaque `HttpOnly` cookie and are stored as SHA-256 hashes in
SQLite. Set `ANIME_SESSION_SECURE=1` only when serving over HTTPS.

Production runs on Railway at `https://anime-srez.up.railway.app`.
Do not use it for development, scraping, indexing, or performance work. See
`instructions/Environment_Rules.md` and
`guides/deployment/railway-production.md` for the environment map and release
workflow.

## Logging

The local server writes rotating log files with Python standard-library
`logging`.

Default location:

```bash
data/logs/
```

The whole `data/` directory is ignored by git.

Override the location for a run or test with:

```bash
export ANIME_LOG_DIR="/path/to/logs"
```

Files:

- `server.log` - server startup, HTTP access lines, and unexpected backend
  exceptions with tracebacks.
- `client-errors.log` - one JSON object per line from browser-side error
  reports.

`POST /api/client-errors` accepts browser error reports without requiring an
authenticated session, so login-page errors can be captured. The endpoint caps
the request body, stores sanitized fields only, and redacts obvious credentials,
tokens, cookies, and third-party player/embed URLs before writing the log.

## Health Checks

- `scripts/check_repo_hygiene.py` rejects tracked or historical local data,
  SQLite databases, logs, env files, caches, and unexpectedly large tracked
  files.
- `scripts/check_data_health.py` validates the ignored local SQLite database,
  the current local backup snapshot, checksums, and the playable-catalog
  invariant.
- `scripts/smoke_dev_app.py` starts the HTTP app on a temporary copy of the
  local database and checks login, auth guards, catalog, and recommendations.

## API

`GET /api/health`

Returns a simple health payload.

`GET /api/auth/config`

Returns the public Google client ID configuration for the login page.

`GET /api/me`

Returns the current authenticated user. Requires a valid session cookie.

`GET /api/admin/users`

Requires the current user's email to match `ANIME_ADMIN_EMAIL`. Returns
registered users, per-user favorite/progress/watched counts, active-session
counts, aggregate summary metrics, and top titles by user activity.

`POST /api/auth/google`

Accepts a Google Identity Services ID token in `{ "credential": "..." }`,
verifies it server-side, upserts the local user, and returns a short-lived
`complete_url`. New Google users start with empty favorites/progress state. The
browser then opens that URL as a top-level page so the server can set the
`HttpOnly` session cookie. The completion page waits until `/api/me` sees that
cookie before returning to the app, with a timed top-level navigation fallback
through `/login?...auth_complete=1` for browser cookie timing quirks. The login
page continuously polls `/api/me` while it is open, uses a faster polling window
after an auth completion handoff, and redirects automatically as soon as the
session cookie becomes visible.

`POST /api/logout`

Revokes the current session and clears the session cookie.

`POST /api/client-errors`

Accepts a JSON browser error report and appends a sanitized JSONL entry to
`client-errors.log`. This endpoint is intentionally available before login.

`GET /api/anime`

Requires authentication. Returns the canonical catalog list with state for the
current user. Source-specific AnimeGO/YummyAnime rows can be merged into one
visible item with `source_variants`. Optional `q` filters the canonical list
across primary and variant titles.

`GET /api/anime/<id-or-slug>`

Requires authentication. Returns one canonical title with genres, dubbings,
fields, source variants, episodes, video sources grouped by episode, and
current-user state. Requests for a canonical `slug`/`internal_id` or a merged
YummyAnime variant ID resolve to the same AnimeGO-primary canonical detail.

`GET /api/recommendations`

Requires authentication. Returns recommended titles plus current-user profile
metadata. Optional `limit` defaults to 20 and is clamped to 1..50. Response
fields include:

- `items` - ranked title rows with `recommendation_score`,
  `recommendation_rank`, `recommendation_confidence`, `recommendation_reasons`,
  and component scores.
- `limit` - normalized limit used by the server.
- `profile` - seed counts, top weighted genres, and mode:
  `personalized` or `popular`.

`GET /api/continue-watching`

Requires authentication. Returns the best current playback target for the user:
the latest in-progress episode, the next available episode after a likely
completed one, or `null` when there is no watch history yet. The frontend uses it
only when the URL does not already point to a specific title.

`PATCH /api/anime/<id>/state`

Requires authentication. Updates local state for the current user. Supported
fields:

- `is_favorite`
- `progress_episode_number`
- `watched`

`POST /api/watch-events`

Requires authentication. Records an observed playback-boundary event for the
current user. The frontend sends these from the iframe boundary and local player
controls: iframe load/focus, window blur when the active element is the player
iframe, fullscreen/PiP, episode/source changes, heartbeat, visibility changes,
and session end. Strong signals update `user_title_state.progress_episode_number`
so the existing manual progress control stays in sync with automatic tracking.

## Data Flow

1. Scrapers fetch upstream pages/API responses.
2. Scrapers normalize title, metadata, episodes, translations, and providers.
3. Scrapers upsert rows into SQLite.
4. `server.py` reads SQLite and emits JSON.
5. `/login` loads Google Identity Services, shows One Tap when available, and
   renders the Google button as a fallback. The login page receives the Google
   ID token in a JavaScript callback and POSTs it to `/api/auth/google`, which
   verifies the token and returns `/api/auth/complete`. The browser opens that
   completion page, receives the local session cookie, waits until `/api/me`
   accepts it when possible, then either opens the requested app route or falls
   back through `/login?...auth_complete=1` so the login page can recover from
   delayed cookie visibility without manual refresh.
6. `static/app.js` loads `/api/me`, `/api/anime`, and
   `/api/continue-watching`, renders the sidebar, and fetches detail JSON when a
   title is selected. Recommendations remain lazy-loaded when the `Советы` view
   needs them.
7. The user can choose the source variant, translation, and provider; the player
   iframe receives the selected provider's `embed_url`.
8. The browser URL is synchronized with the right-pane state using the canonical
   title slug in the path and query params for `episode`, `source`,
   `translation`, and `provider`.
9. Favorites and manual progress corrections are written back through
   `PATCH /api/anime/<id>/state`.
10. Automatic watch signals are written through `POST /api/watch-events`, which
   appends raw events, updates per-episode aggregate state, and updates the same
   per-title progress summary used by the manual controls.
11. Recommendation data is reloaded after favorite/progress/watched changes.

## Scraper Notes

AnimeGO:

- Listing pages provide title cards, listing score, type/year tags, genres, and
  detail links.
- Detail pages provide richer metadata, JSON-LD rating data, fields, genres,
  dubbings, description, and an AJAX player shell URL.
- Player endpoints expose episodes, translations, providers, and player URLs.
- Production catalog imports must persist playable embed URLs with
  `--include-embed-urls`; `--skip-player` is only a metadata research mode.
- `backfill_players.py` can fill missing player rows after a broad catalog
  scrape. Its default fetches all exposed episodes; use `--episode-limit` only
  for temporary samples. `prune_non_playable.py --commit` removes rows that
  still have no playable `embed_url`.
- `sync_videos.py --mode hourly` is the light periodic path: YummyAni feed plus
  small AnimeGO ongoing/missing coverage checks.
- `sync_videos.py --mode daily` checks broader ongoing coverage and can find
  newly added voices/providers without rewriting existing rows.

YummyAnime/YummyAni:

- Legacy `yummyanime.tv` pages use page HTML plus AJAX player parameters.
- Modern `ru.yummyani.me` catalog pages can be mapped through `api.yani.tv`.
- Internal title IDs are offset by `10_000_000` to avoid collisions with
  AnimeGO IDs.
- YummyAnime translation IDs use reserved high ranges.
- Alloha is intentionally skipped for YummyAnime imports; Kodik is preferred
  when available.
- `--no-embed-urls` and `--skip-player` are metadata research modes and should
  be followed by player backfill before the database is used by the app.

## Frontend Filtering and Sorting

Filtering and sorting are config-driven in `static/app.js`.

Current filters:

- Genre.
- Year.
- Type.
- Status.
- Source.
- Video availability.

The source filter matches any source variant inside a canonical item, not only
the primary source.

Current sorts:

- Best rating.
- Site rating.
- Listing rating.
- Rating count.
- Year.
- Video availability.
- Title.

The filter option lists are computed from the loaded catalog and include counts.
Catalog search is ranked client-side while the catalog remains small. It
normalizes Russian text, folds common Japanese transcription variants, matches
tokens out of order, and allows small typos on longer words.

## Recommendations

Recommendation scoring lives in `server.py` and is intentionally explainable.
It is scoped to the current authenticated user:

- Favorites count most strongly as taste seeds.
- Watched titles and titles with progress count as weaker seeds.
- Candidates already favorited, watched, or in progress are excluded.
- Genre overlap and nearest-seed similarity form the taste score.
- Rating, rating count, source availability, recency, and type match adjust the
  final ranking.
- Recommendations expect the main catalog to be watchable-only. As a defensive
  fallback, if metadata-only rows leak into the database, watchable candidates
  are kept above rows that cannot be opened in the player.
- With no seeds, the endpoint uses a popularity/quality/availability fallback.

The frontend always requests 20 items and displays them in the `Советы` tab.
Each row shows score/confidence/source availability and short reason text. The
profile metadata includes total candidate count and watchable candidate count.

## Player Controls

The local fullscreen button calls `requestFullscreen()` on the iframe wrapper.
The iframe also carries `allow="fullscreen; picture-in-picture"` so the embedded
player can request those permissions.

The PiP button uses `window.documentPictureInPicture` when the browser supports
it. If unavailable or blocked, the UI reports that PiP is available inside the
embedded player. The local app cannot directly control a cross-origin player's
internal video element.

Automatic watch tracking therefore attaches to the iframe boundary, not to the
third-party player's internal `video.play`, `timeupdate`, or `ended` events.
`iframe load` is stored as low-confidence history only. Progress starts moving
after stronger user signals such as iframe focus, fullscreen/PiP, explicit
episode selection, source changes, or heartbeat after engagement.

## Shared Links

Right-pane state is shareable through the address bar. Opening a URL such as
`/reinkarnaciya-bezrabotnogo-3-sezon-5yc9?episode=43793&source=animego&translation=95&provider=110`
restores that canonical title, episode, source variant, translation, and
provider. Older `/?anime=<numeric-id>` links still load as a compatibility
fallback and are normalized to the slug URL. Invalid or stale params fall back
to the closest available option and the address bar is normalized to the actual
current state.

## Verification

Use this command set after behavior changes:

```bash
.venv/bin/python -m py_compile server.py scrape_animego.py scrape_yummyanime.py sync_videos.py backfill_players.py prune_non_playable.py update_backup.py test_app.py scripts/check_repo_hygiene.py scripts/check_data_health.py scripts/smoke_dev_app.py
.venv/bin/python -m unittest -v test_app.py test_db_migrate.py
node --check static/app.js
node --check static/login.js
node --check static/admin.js
```

For UI changes, also do a browser smoke test against dev:
`http://127.0.0.1:8765/`.
At minimum, verify catalog load, filtering/sorting, detail selection, and player
source selection for a title with video.

For recommendation/player changes, additionally verify:

- `/api/recommendations?limit=20` returns 20 ranked items or fewer when the
  catalog has fewer candidates.
- The `Советы` tab shows reason text and profile metadata.
- Fullscreen enters on `#iframe-wrap` and exits cleanly.
- PiP opens with document Picture-in-Picture in Chrome or shows the fallback
  message.
- Fast consecutive state changes, such as progress then watched, do not drop the
  second update.

If `http://127.0.0.1:8765/` serves new static files but API routes return 404,
restart the long-running `server.py` process. Python route handlers are loaded
only when the process starts.

Production smoke checks are only part of an explicit release operation. Do not
use Railway production as a substitute for dev testing.
