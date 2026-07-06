# Project Overview

Anime Local is a local anime catalog and player prototype. It scrapes metadata
from anime sites into SQLite, serves the catalog from a tiny Python HTTP server,
and renders a compact browser UI for browsing, filtering, selecting episodes,
choosing a provider/translation, and tracking personal progress.

## Current User Workflows

- Browse all scraped titles.
- Search by title, subtitle, source, type, status, year, or genre.
- Filter by genre, year, type, status, source, and whether video is available.
- Sort by best rating, site rating, listing rating, rating count, year, video
  availability, or title.
- Open a title detail view with poster, metadata, description, genres, fields,
  episodes, and player source selectors.
- Add a title to favorites.
- Store current episode progress.
- Mark a title as watched.
- View only favorites or titles with progress.
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

As of the current local SQLite snapshot on 2026-07-06, the database contains:

- 4,036 source title rows, exposed as 3,202 canonical catalog titles.
- 1,170 AnimeGO source titles.
- 2,866 YummyAnime/YummyAni source titles.
- 212 episode rows.
- 175 video source rows.
- 16,011 genre rows.
- 24 user progress/favorite rows.

The committed recovery snapshot is in `backups/current/`. It includes a full
SQLite backup and readable exports of favorites/progress/watched state.

These counts are not product constants. They are a local database snapshot and
will change after scraping or user activity.

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

Recommendations are computed on demand from local data. Favorites have the
highest profile weight; watched titles and titles with progress also contribute.
The score combines genre similarity, similarity to known titles, rating quality,
source availability, popularity, recency, and title type. Known titles already
favorited, watched, or in progress are excluded.

When no taste profile exists yet, the same endpoint falls back to a popularity
and availability ranking.

## Known Limitations

- The app is a local prototype, not an authenticated multi-user service.
- Filtering and sorting are client-side because the catalog size is currently
  small enough for the full list to load at once.
- Recommendations use only local metadata and user state; there is no external
  AniList/MAL similarity graph yet.
- Scraper parsing is coupled to upstream HTML/API shapes.
- External embeds can fail independently of the local app, and their internal
  fullscreen/PiP behavior is controlled by the external player.
- The current UI intentionally favors compact browsing over a polished media
  library experience.
