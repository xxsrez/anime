# AnimeGO scraper notes

Small local scraper prototype for `https://animego.me/anime/status/ongoing`.

Project documentation lives in `docs/`. Start with `docs/README.md`.

## What is scraped

- Ongoing catalog pages.
- Title metadata: title, poster, type/year/status, genres, dubbings, description.
- Player metadata from `/player/<anime_id>` and `/player/videos/<episode_id>`.
- Episode rows and provider/translation availability.

The scraper does not call `/embed/` URLs and does not download video streams.
By default it also does not persist live third-party embed URLs. Use
`--include-embed-urls` only for sources you are allowed to embed.

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
python3 scrape_animego.py --limit 5 --episode-limit 2
```

The default database path is `data/animego.sqlite`.

The current committed recovery snapshot is stored in `backups/current/`. To
restore the full local catalog database:

```bash
cp backups/current/animego.sqlite data/animego.sqlite
sqlite3 data/animego.sqlite 'pragma integrity_check;'
```

For a working iframe prototype, explicitly persist embed URLs:

```bash
python3 scrape_animego.py --pages 2 --limit 20 --episode-limit 3 --include-embed-urls
python3 server.py
```

Then open `http://127.0.0.1:8765`.

The local app stores favorites and watch progress in SQLite table
`user_title_state`. Use the title page controls to add a title to favorites,
set the current episode, or mark the title as watched. The sidebar can filter
all titles, favorites, titles with progress, or the top recommendation list.
Recommendations are computed from favorites/progress/watched state and show the
20 strongest candidates first with short reasons.

Scrape yearly metadata without touching player endpoints:

```bash
python3 scrape_animego.py --start-url https://animego.me/anime/season/2025 --all-pages --limit 0 --skip-player
python3 scrape_animego.py --start-url https://animego.me/anime/season/2026 --all-pages --limit 0 --skip-player
```

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
sqlite3 data/animego.sqlite '
select count(*) as anime from anime;
select count(*) as episodes from episodes;
select count(*) as video_sources from video_sources;
'
```

Inspect local recommendations:

```bash
curl 'http://127.0.0.1:8765/api/recommendations?limit=20'
```
