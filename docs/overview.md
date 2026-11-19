# Project Overview

Anime Local is a local anime catalog and player prototype. It scrapes metadata
from anime sites into SQLite, serves the catalog from a tiny Python HTTP server,
and renders a compact browser UI for browsing, filtering, selecting episodes,
choosing a provider/translation, and tracking personal progress.

## Current User Workflows

- Browse all scraped titles.
- Sign in with a Google account through One Tap or the Google button and see
  the active account in the sidebar.
- Sign out from the sidebar account control.
- Search by title, subtitle, source, type, status, year, or genre.
- Filter by genre, year, type, status, source, and whether video is available.
- Sort by MyAnimeList score, site rating, listing rating, rating count, year, video
  availability, or title.
- Open a title detail view with poster, metadata, description, genres, fields,
  episodes, and player source selectors.
- Open a franchise hub from a related title, compare release and recommended
  watch orders, see optional films/OVA/spin-offs, and spot known parts that are
  not yet in the playable catalog.
- Add a title to favorites independently of its viewing status.
- Store current episode progress manually or through automatic watch signals
  around the embedded player.
- Choose exactly one viewing status: `Не смотрю`, `Смотрю`, or
  `Просмотрено`.
- View only favorites or titles with progress.
- Resume the latest in-progress title on the home page when there is no explicit
  title in the URL.
- View the top 20 recommended titles in `Советы`, sorted by estimated fit and
  shown with short reasons.
- Open the player wrapper in fullscreen or request Picture-in-Picture when the
  browser supports document-level PiP.

## Current Data Sources

The project currently knows about two source families:

- AnimeGO metadata and player metadata.
- YummyAnime/YummyAni title metadata imported into the same catalog model.
- A canonical API layer merges conservative AnimeGO/YummyAnime duplicate
  matches into one visible title with multiple source variants.
- A curated franchise layer groups those canonical releases without relying on
  unsafe title similarity and can include missing or future releases.

Catalog counts are intentionally not fixed in documentation. Inspect the
current dev snapshot with `scripts/check_data_health.py`; production has its own
cron and may be newer. User progress/favorite rows are per Google user. New
Google accounts start empty; anonymous/local profile state is not supported.

The local recovery snapshot is in `db/backups/current/`. It includes a full
SQLite backup and readable exports of favorites/progress/watched state. The
`db/` directory is intentionally ignored by git.

The local snapshot changes after scraping or user activity and is not expected
to match production row-for-row.

## Playback Model

The local app does not host video. It embeds third-party players in an iframe
using URLs saved in SQLite. The main catalog is a watchable catalog: imported
rows that still have no playable `embed_url` after backfill are pruned instead
of being shown as metadata-only titles. For YummyAnime, the importer keeps Kodik
embeds and skips Alloha embeds because the Alloha AJAX response can return stale
or mismatched player URLs.

Fullscreen is implemented on the local iframe wrapper. Picture-in-Picture uses
Chrome's document Picture-in-Picture API when available and otherwise falls back
to the embedded player's own PiP controls.

## Recommendation Model

Recommendations are computed on demand from local data for the current Google
user. Favorites have the highest profile weight; `completed` titles and
meaningful watch history also contribute. The score combines genre similarity,
similarity to known titles, the public MyAnimeList score and vote count,
playability, and title type. Known titles already favorited, completed, or in
progress are excluded.

The favorite flag and the three-state viewing status are independent. The
internal `not_interested` signal is reserved for recommendation feedback; it is
not shown as a fourth library status or title-page action.

Without a taste profile, the endpoint ranks by MAL score, playability, and MAL
vote count. Every card displays its MAL score out of 10 or a dash. The
recommendation score out of 100 uses the same MAL score and treats missing
quality as neutral. The catalog's MyAnimeList sort puts unrated titles last
in both directions.

### Refresh MyAnimeList ratings

Download `anime-offline-database.jsonl` from the
[Anime Offline Database releases](https://github.com/cedya77/anime-offline-database/releases)
to the ignored `data/mal-ratings/` directory. From the repository root, run:

```bash
.venv/bin/python scripts/import_mal_ratings.py \
  --db db/animego.sqlite \
  --aod data/mal-ratings/anime-offline-database.jsonl --apply
```

The script reads MAL scores and vote counts from Tenrai using verified IDs.
Explicit MAL IDs take precedence. For existing cards without an ID, it
checks titles and release years, with Russian-name lookup via Shikimori where
needed. The write is transactional. Omit `--apply` to inspect coverage or add
`--missing-only` to process new and previously unmatched rows. Run the full
refresh to revisit titles that previously lacked a public score. Pass
`--verified-ids PATH` to include an independently verified JSON mapping from
`source:source_id` to MAL ID for titles with alternate names. A null mapping
leaves a combined card out of the import when MAL rates its parts separately.

## Known Limitations

- The app uses local Google Sign-In sessions, but it is still a local prototype
  rather than a hardened public multi-user service.
- Filtering and sorting are client-side because the catalog size is currently
  small enough for the full list to load at once.
- Recommendations use only local metadata and user state; there is no external
  AniList/MAL similarity graph yet.
- Scraper parsing is coupled to upstream HTML/API shapes.
- External embeds can fail independently of the local app, and their internal
  fullscreen/PiP behavior is controlled by the external player.
- The current UI intentionally favors compact browsing over a polished media
  library experience.
