# Technical Documentation

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
- `static/index.html` defines the local app shell.
- `static/app.js` owns client state, rendering, filtering, sorting, player
  source selection, recommendations, player controls, and user-state PATCH
  calls.
- `static/app.css` owns the compact dark UI.
- `test_app.py` contains the current regression tests.

## Local Server

Run the dev/test server:

```bash
python3 server.py --host 127.0.0.1 --port 8765
```

The default database is `data/animego.sqlite`. Override it with:

```bash
python3 server.py --db /path/to/animego.sqlite
```

or with the `ANIMEGO_DB` environment variable for internal helper calls.

Authentication uses Sign in with Google. The login page calls Google One Tap
first and keeps the Google button as a fallback. Both paths use the popup
callback ID-token flow, then POST the credential to the local server and finish
through a top-level completion page that sets the session cookie. Google
requires a public OAuth client ID:

```bash
export GOOGLE_CLIENT_ID="...apps.googleusercontent.com"
```

Optional restrictions:

```bash
export ANIME_AUTH_ALLOWED_EMAILS="one@example.com,two@example.com"
export ANIME_AUTH_ALLOWED_DOMAINS="example.com"
```

Sessions use an opaque `HttpOnly` cookie and are stored as SHA-256 hashes in
SQLite. Set `ANIME_SESSION_SECURE=1` only when serving over HTTPS.

Production is a separate localhost environment at `http://127.0.0.1:8766/`.
Do not use it for development, scraping, indexing, or performance work. See
`ENVIRONMENTS.md` for the fixed port map and release workflow.

## API

`GET /api/health`

Returns a simple health payload.

`GET /api/auth/config`

Returns the public Google client ID configuration for the login page.

`GET /api/me`

Returns the current authenticated user. Requires a valid session cookie.

`POST /api/auth/google`

Accepts a Google Identity Services ID token in `{ "credential": "..." }`,
verifies it server-side, upserts the local user, and returns a short-lived
`complete_url`. The browser then opens that URL as a top-level page so the
server can set the `HttpOnly` session cookie. The completion page waits until
`/api/me` sees that cookie before returning to the app.

`POST /api/logout`

Revokes the current session and clears the session cookie.

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

`PATCH /api/anime/<id>/state`

Requires authentication. Updates local state for the current user. Supported
fields:

- `is_favorite`
- `progress_episode_number`
- `watched`

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
   accepts it, and is sent back to the requested app route.
6. `static/app.js` loads `/api/me`, `/api/anime`, and `/api/recommendations`,
   renders the sidebar, and fetches detail JSON when a title is selected.
7. The user can choose the source variant, translation, and provider; the player
   iframe receives the selected provider's `embed_url`.
8. The browser URL is synchronized with the right-pane state using the canonical
   title slug in the path and query params for `episode`, `source`,
   `translation`, and `provider`.
9. Favorites and progress are written back through `PATCH /api/anime/<id>/state`.
10. Recommendation data is reloaded after favorite/progress/watched changes.

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
  scrape. `prune_non_playable.py --commit` removes rows that still have no
  playable `embed_url`.

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
Search normalizes `ё` to `е` and `э` to `е`, so a query like `исекай` can match
the scraped tag `Исэкай`.

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
python3 -m py_compile server.py scrape_animego.py scrape_yummyanime.py backfill_players.py prune_non_playable.py update_backup.py test_app.py
python3 -m unittest -v test_app.py
node --check static/app.js
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

Prod smoke checks are only part of an explicit release operation. Do not use
`http://127.0.0.1:8766/` as a substitute for dev testing.
