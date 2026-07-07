# Code Style And Project Rules

## General

- Keep the project dependency-light.
- Prefer small functions over framework-level abstractions.
- Keep local behavior explicit and easy to inspect.
- Do not download or store video streams.
- Treat `data/animego.sqlite` as local mutable state.
- Keep source-specific quirks isolated in the relevant scraper.
- Run the relevant checks before declaring work complete.
- Keep environment discipline strict: `8765` is dev/test/scratch, `8766` is
  prod, and `8776` is retired.

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
- When filters change and the selected title is no longer visible, select the
  first title in the new filtered list.

## CSS And UI

- Keep the app compact and work-focused.
- Prefer dense controls in the sidebar over large decorative areas.
- Use restrained dark UI colors already present in the project.
- Keep controls at stable heights so filtering/sorting does not shift layout
  unexpectedly.
- Avoid nested cards. Use panels only for genuinely framed tools such as the
  player source selector.
- Keep mobile behavior simple: sidebar above detail, player controls stacked.

## Tests And Verification

Use dev (`http://127.0.0.1:8765/`) for all regular verification, browser
testing, scraping, indexing, and performance checks. Prod
(`http://127.0.0.1:8766/`) is release-only and must not be changed, restarted,
or used as scratch unless the user explicitly asks for a prod operation.

For code changes, run:

```bash
python3 -m py_compile server.py scrape_animego.py scrape_yummyanime.py sync_videos.py backfill_players.py prune_non_playable.py update_backup.py test_app.py
python3 -m unittest -v test_app.py
node --check static/app.js
node --check static/login.js
```

For UI changes, also verify in a browser:

- Unauthenticated app routes redirect to `/login`.
- Google-authenticated sessions show the account row and logout works.
- Catalog loads.
- Search works.
- Filters narrow the list.
- Sorting changes order.
- Reset restores the full list.
- Detail view stays synchronized with the filtered list.
- At least one known video title still opens a player iframe.
- The `Советы` tab loads 20 ranked recommendations with reasons.
- Fullscreen and Picture-in-Picture controls show a clear state or fallback.
- Fast user-state changes do not drop queued updates.

Do not report checks as passing unless they were actually run in the current
work session.
