# Code Style And Project Rules

## General

- Keep the project dependency-light.
- Prefer small functions over framework-level abstractions.
- Keep local behavior explicit and easy to inspect.
- Do not download or store video streams.
- Treat `db/animego.sqlite` as local mutable state.
- Keep `db/`, `data/`, local backups, logs, virtualenvs, and generated caches
  out of git.
- Keep source-specific quirks isolated in the relevant scraper.
- Run the relevant checks before declaring work complete.
- Keep environment discipline strict: `8765` is dev/test/scratch, Railway is
  production, and local ports `8766`/`8776` are retired.

## Python

- Use Python standard library facilities where they are enough.
- Use `argparse` for runnable scripts.
- Use `pathlib.Path` for local paths.
- Use timezone-aware UTC timestamps via ISO strings.
- Keep scraper parsing in named helper functions.
- Keep database writes idempotent with `insert ... on conflict`.
- Use SQLite migrations through additive schema checks when the local server
  needs to tolerate an older database.
- Keep source IDs namespaced when importing from multiple sites.
- Redact token-like embed URLs where possible.

## Scraping Rules

- Always send a realistic `User-Agent`.
- Use delays when scraping larger ranges.
- Do not assume upstream HTML is stable.
- Store enough metadata to debug source behavior later.
- Production catalog imports must persist playable embed URLs. `--skip-player`
  and `--no-embed-urls` are metadata-only research modes and must not be mixed
  into the main app database without a player backfill and non-playable prune.
- For YummyAnime, skip Alloha sources unless a future fix proves they are stable;
  the current importer keeps Kodik because it has been verified in the local app.

## JavaScript

- Use plain browser JavaScript.
- Keep global app state in the `state` object.
- Keep DOM references in the `el` object.
- Prefer config arrays for extensible UI behavior such as filters and sorts.
- Keep render functions deterministic from `state`.
- Keep recommendation UI explainable: every suggested title should have short
  visible reasons and stable ranking metadata.
- Keep filtering/sorting client-side while the catalog remains small.
- Normalize Russian search text enough for practical lookup, for example
  `исекай` matching `Исэкай`.
- Catalog filtering never opens a title or starts a player. List views and
  title/franchise screens are independent; browser history restores the view,
  query, filters, and sorting.

## CSS And UI

- Keep the app compact and work-focused.
- The navigation rail contains Catalog, Library, and service links. Catalog has
  All, Recommendations, and New; Library has All, Watching, Favorites, and
  Completed. Favorites are independent of watch status; Library All is their
  deduplicated union with Watching and Completed.
- Use the restrained light/dark palettes in `static/redesign.css`. Theme choice
  persists across catalog, title, franchise, login, admin, and scanner setup.
- Keep controls at stable heights so filtering/sorting does not shift layout
  unexpectedly.
- Avoid nested cards. Use panels only for genuinely framed tools such as the
  player source selector.
- Use one document scroll on mobile, with navigation above the active screen.
  Keep the player full width, compact episode/source selectors below it, and
  favorite/status/share actions together above it. Description stays expanded.

## Tests And Verification

Use dev (`http://127.0.0.1:8765/`) for all regular verification, browser
testing, scraping, indexing, and performance checks. Railway production is
release-only and must not be changed, redeployed, or used as scratch unless the
user explicitly asks for a production operation.

For code changes, run:

```bash
ruff check .
pip-audit -r requirements.txt
.venv/bin/python scripts/check_repo_hygiene.py
.venv/bin/python -m unittest discover -v -p 'test*.py'
find static -name '*.js' -print0 | xargs -0 -n1 node --check
node --test static/frontend_runtime.test.js
```

For local database or backup changes, also run:

```bash
.venv/bin/python scripts/check_data_health.py
.venv/bin/python scripts/smoke_dev_app.py
```

For UI changes, also verify in a browser:

- Unauthenticated app routes redirect to `/login`.
- Google-authenticated sessions show the account row and logout works.
- Catalog loads.
- Search works.
- Filters narrow the list.
- Sorting changes order.
- Reset restores the full list.
- List filters never auto-select another title. Back restores list criteria.
- At least one known video title still opens a player iframe.
- The `Рекомендации` tab loads 20 ranked recommendations with reasons.
- Fullscreen and Picture-in-Picture controls show a clear state or fallback.
- Fast user-state changes do not drop queued updates.

Do not report checks as passing unless they were actually run in the current
work session.
