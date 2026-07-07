# AnimeGO scraper notes

Small local scraper prototype for `https://animego.me/anime/status/ongoing`.

Project documentation lives in `docs/`. Start with `docs/README.md`.

## What is scraped

- Ongoing catalog pages.
- Title metadata: title, poster, type/year/status, genres, dubbings, description.
- Player metadata from `/player/<anime_id>` and `/player/videos/<episode_id>`.
- Episode rows and provider/translation availability.

The scraper does not call `/embed/` URLs and does not download video streams.
The local watchable catalog does persist third-party embed URLs so every visible
title can open a player.

## Player model found

AnimeGO title pages render an empty player shell:

- The title page contains `.player__video` with `data-ajax-url="/player/<anime_id>"`.
- `/player/<anime_id>` returns JSON with `data.content`, which is HTML for the
  player menu, episode list, translations, and providers.
- Episode switching calls `/player/videos/<episode_id>`.
- Translation buttons use `data-translation="<translation_id>"`.
- Provider buttons use:
  - `data-provider`
  - `data-provider-title`
  - `data-ptranslation`
  - `data-translation-title`
  - `data-player`
- The frontend creates an iframe and sets `src` to `data-player`.
- Choosing a translation filters provider buttons by matching `data-ptranslation`.

## Run

```bash
python3 sync_videos.py --mode hourly
```

The default database path is `db/animego.sqlite`.

The local recovery snapshot is stored in `db/backups/current/`. `db/` is
ignored by git so the mutable SQLite database and backups stay out of the
repository. To restore the full local catalog database:

```bash
cp db/backups/current/animego.sqlite db/animego.sqlite
sqlite3 db/animego.sqlite 'pragma integrity_check;'
```

For a working iframe prototype or full dev catalog refresh, persist embed URLs
and then prune rows where the source page still exposes no player:

```bash
python3 sync_videos.py --mode daily
python3 prune_non_playable.py --commit
.venv/bin/python server.py
```

Then open the dev site at `http://127.0.0.1:8765`.

The stable local production site is `http://127.0.0.1:8766/`. It is release-only
and must not be used for scraping, indexing, experiments, or routine testing.
See `docs/ENVIRONMENTS.md` for the fixed dev/prod split and release workflow.

The local app requires Sign in with Google. The login page shows Google One Tap
when available and keeps the Google button as a fallback. `server.py` loads a
repo-local `.env` file by default, so set the public `GOOGLE_CLIENT_ID` there or
export it before starting the server; optionally restrict access with
`ANIME_AUTH_ALLOWED_EMAILS` or `ANIME_AUTH_ALLOWED_DOMAINS`.
The OAuth client must be a Google Cloud `Web application` client with the dev
origin registered, for example `http://127.0.0.1:8765` and/or
`http://localhost:8765`.

The local app stores favorites and watch progress per Google user in SQLite
table `user_title_state`. Use the title page controls to add a title to
favorites, set the current episode, or mark the title as watched. The sidebar
can filter all titles, favorites, titles with progress, or the top
recommendation list. Recommendations are computed from the current user's
favorites/progress/watched state and show the 20 strongest candidates first
with short reasons. A newly created Google user starts with empty local state;
anonymous/local profile state is not supported.

Refresh the watchable catalog with player data:

```bash
python3 sync_videos.py --mode hourly
python3 sync_videos.py --mode daily
python3 prune_non_playable.py --commit
```

`sync_videos.py` is video-first: it skips metadata-only title/episode rows by
default, fetches all exposed episodes with `--episode-limit 0`, and does not
rewrite already known video source rows unless `--refresh-known` is passed.
Hourly mode is the light update path; daily/full mode checks broader ongoing
coverage and can find newly added voices/providers. `--skip-player`,
`--no-embed-urls`, and `--include-empty-episodes` are research/enrichment modes;
do not use them for the main local catalog unless a backfill/prune step follows.

Scrape the currently available YummyAnime pages for the Mushoku Tensei /
`Реинкарнация безработного` franchise into the same database:

```bash
python3 scrape_yummyanime.py
```

The current YummyAnime franchise block exposes four available pages: season 1,
the Eris special, the Fitz special, and season 3. Older indexed URLs for
additional parts currently resolve to the site's unavailable-page response.
For YummyAnime, the importer keeps Kodik embeds and skips Alloha embeds because
the Alloha AJAX response can return stale or mismatched player URLs.

Inspect a quick summary:

```bash
sqlite3 db/animego.sqlite '
select count(*) as anime from anime;
select count(*) as episodes from episodes;
select count(*) as video_sources from video_sources;
'
```

Inspect local recommendations:

```bash
curl 'http://127.0.0.1:8765/api/recommendations?limit=20'
```

Run local health and smoke checks:

```bash
python3 scripts/check_repo_hygiene.py
python3 scripts/check_data_health.py
python3 scripts/smoke_dev_app.py
```
