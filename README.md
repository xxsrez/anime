# AnimeGO scraper notes

Small local scraper prototype for `https://animego.me/anime/status/ongoing`.

Project documentation lives in `docs/`. Start with `docs/README.md`; for dev
server management, Railway production, release steps, env vars, and login
troubleshooting, use `docs/instructions/Operations_Runbook.md` as the central
runbook.

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
.venv/bin/python sync_videos.py --mode hourly
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
.venv/bin/python sync_videos.py --mode daily
.venv/bin/python prune_non_playable.py --commit
.venv/bin/python server.py
```

Then open the dev site at `http://127.0.0.1:8765`.

Production runs on Railway at `https://anime-srez.up.railway.app`.
It is release-only and must not be used for scraping, indexing, experiments, or
routine testing. See `docs/instructions/Environment_Rules.md` and
`docs/guides/deployment/railway-production.md` for the dev/prod split and
release workflow.

The local app requires Sign in with Google. The login page shows Google One Tap
when available and keeps the Google button as a fallback. `server.py` loads a
repo-local `.env` file by default, so set the public `GOOGLE_CLIENT_ID` there or
export it before starting the server; optionally restrict access with
`ANIME_AUTH_ALLOWED_EMAILS` or `ANIME_AUTH_ALLOWED_DOMAINS`.
Set `ANIME_ADMIN_EMAIL` to the single Google email that may open `/admin`; if
`ANIME_AUTH_ALLOWED_EMAILS` is set, include the same email there too.
Set a durable random `ANIME_GOOGLE_AUTH_STATE_SECRET` of at least 32 bytes in
production so Google callbacks remain valid across restarts or replicas.
The OAuth client must be a Google Cloud `Web application` client with the dev
origin registered, for example `http://127.0.0.1:8765` and/or
`http://localhost:8765`.

The local app stores favorites, watch progress, and the last opened episode per
Google user in SQLite. `user_title_navigation_state` remembers which episode to
reopen without treating browsing as watching. `user_title_state` is the current
per-title summary, while `user_watch_events` and `user_episode_state` keep
automatic watch history around the embedded player. Use the title page controls
to add a title to favorites, correct the current episode manually, or choose one
mutually exclusive library status:
`Не смотрю`, `Смотрю`, or `Просмотрено`. The favorite flag is independent
of that status, so all six favorite/status combinations are valid. Automatic
iframe-focus, fullscreen/PiP, and engaged heartbeat signals update progress;
episode and source selection alone do not. The sidebar can filter all titles,
favorites, titles with progress, or the top recommendation list. Recommendations are
computed from the current user's favorites, titles marked `Просмотрено`, and
meaningful watch history from the player; a short accidental visit does not
become a taste seed. Favorites and explicitly completed titles are weighted much
higher than automatic watch history. `not_interested` remains an internal
recommendation-feedback signal and is not exposed as a title-page status. A
newly created Google user starts with empty local state; anonymous/local profile
state is not supported. The `Новое` view lifts favorite and currently watched
titles only while their update report contains a new episode beyond the user's
current progress; completed and caught-up titles stay in normal chronological
order.

Refresh the watchable catalog with player data:

```bash
.venv/bin/python sync_videos.py --mode hourly
.venv/bin/python sync_videos.py --mode daily
.venv/bin/python prune_non_playable.py --commit
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
.venv/bin/python scrape_yummyanime.py
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
.venv/bin/python -m unittest discover -v -p 'test*.py'
node --test static/frontend_runtime.test.js
.venv/bin/python scripts/check_repo_hygiene.py
.venv/bin/python scripts/check_data_health.py
.venv/bin/python scripts/smoke_dev_app.py
```

Production has an independent content-sync cron and can legitimately contain
newer episodes than dev. Treat that freshness difference as expected; never
replace production with the older local database just to align row counts.
YummyAnime is fetched directly by the Railway web sync. AnimeGO blocks the
current cloud egress, so production AnimeGO updates are collected by the
trusted push worker documented in `docs/instructions/Operations_Runbook.md`;
the production web process still performs all validation and SQLite writes.
