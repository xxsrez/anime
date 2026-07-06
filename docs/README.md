# Anime Local Documentation

This directory is the durable project documentation for the local anime catalog
and player prototype.

## Reading order

1. `OVERVIEW.md` - product summary, current scope, user workflows, limitations.
2. `ENVIRONMENTS.md` - localhost dev/prod split, fixed ports, release rules.
3. `TECHNICAL.md` - runtime, data flow, API, scraper behavior, verification.
4. `DATA_MODEL.md` - SQLite tables, relationships, ID conventions, mutable data.
5. `CODE_STYLE.md` - project coding rules and contribution conventions.

## Quick start

Run the dev/test site:

```bash
python3 server.py --port 8765
```

Open `http://127.0.0.1:8765/`.

Production is fixed at `http://127.0.0.1:8766/` and must only be updated after
an explicit release request. See `ENVIRONMENTS.md` before touching prod.

Run the current verification set:

```bash
python3 -m py_compile server.py scrape_animego.py scrape_yummyanime.py backfill_players.py prune_non_playable.py update_backup.py test_app.py
python3 -m unittest -v test_app.py
node --check static/app.js
```

After frontend or API changes, restart any already-running `server.py` process.
The static files are read fresh, but Python route handlers are not reloaded in a
long-running process.

## Important boundaries

- The project stores catalog/player metadata in local SQLite.
- Dev/test/scratch is `8765`; prod is `8766`; port `8776` is retired.
- The recommendation list is computed locally from `user_title_state` and
  catalog metadata.
- The project does not download or host anime video streams.
- Playback uses third-party embed URLs saved in SQLite. The main catalog should
  not contain titles that have no playable `embed_url`.
- `data/animego.sqlite` is local mutable state and can change after scraping or
  user progress updates.
- Scraping depends on upstream site structure and can break when those sites
  change.
