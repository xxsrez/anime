# Data Model

The project uses one SQLite database, `db/animego.sqlite`, for scraped catalog
data and local user state.

## Tables

`anime`

One row per scraped source title, not necessarily one visible catalog title.
AnimeGO and YummyAnime rows can describe the same anime and are kept separate
so scraper refreshes remain source-specific. The main app database is pruned to
watchable source rows, so each retained title should have at least one
`video_sources.embed_url`. Important fields:

- `id` - internal title ID.
- `source` - source namespace, for example `animego` or `yummyanime`.
- `source_id` - original upstream ID as text.
- `slug`, `title`, `subtitle`, `url`, `cover_url`.
- `kind`, `year`, `status`, `episodes_text`.
- `listing_score`, `aggregate_score`, `aggregate_count`.
- `date_published`, `rating`, `age`, `duration`, `studio`, `season`.
- `description`.
- `fields_json`, `schema_json` for richer source payloads.
- `scraped_at`.

`episodes`

One row per known episode:

- `id` - internal episode ID.
- `anime_id` - parent title.
- `number`, `title`, `release_label`, `episode_type`, `description`.
- `has_video`.
- `unavailable_reason`.
- `scraped_at`.

`video_sources`

One row per provider/translation/embed combination:

- `anime_id`, `episode_id`.
- `provider_id`, `provider_title`.
- `translation_id`, `translation_title`.
- `embed_host`.
- `embed_url`.
- `embed_url_redacted`.
- `scraped_at`.

The uniqueness key is `(episode_id, provider_id, translation_id,
embed_url_redacted)`. This allows multiple providers/translations per episode
without duplicating the same source.

`anime_genres`

Many-to-many title genres.

`anime_dubbings`

Many-to-many title dubbing labels.

`anime_fields`

Normalized label/value metadata extracted from source detail pages.

`translations`

Translation labels discovered by the AnimeGO scraper.

`scrape_runs`

Audit table for scraper runs.

`users`

One row per authenticated Google account:

- `id` - local user ID.
- `google_sub` - stable Google subject ID, unique. Google `sub` is the durable
  identity key; email is not used as the primary identity.
- `email`, `email_verified`, `name`, `picture_url`.
- `created_at`, `last_login_at`.

`sessions`

Opaque local sessions:

- `token_hash` - SHA-256 hash of the browser session token.
- `user_id`.
- `created_at`, `expires_at`, `revoked_at`, `last_seen_at`.

`user_title_state`

Per-user mutable state:

- `user_id`.
- `anime_id`.
- `is_favorite`.
- `progress_episode_number`.
- `watch_status`: exactly one of `none`, `watching`, or `completed`.
- `watched`: backward-compatible value derived from `watch_status`.
- `not_interested`: hidden recommendation feedback, not a library status.
- `updated_at`.

`is_favorite` is independent of `watch_status`, so each of the three statuses
can exist with or without a favorite. `not_interested` is separate from those
six user-visible combinations; setting a favorite clears contradictory negative
feedback.

The primary key is `(user_id, anime_id)`. The recommendation endpoint reads this
table together with `user_episode_state` for the current-user taste profile.
Favorites and explicitly completed titles are strongest, while automatic watch
history only counts after an episode has at least five engaged minutes and uses
a lighter seed weight.
In-progress titles from short visits still support progress/continue-watching
but do not become taste seeds. Recommendations themselves are computed at request
time and are not persisted. New Google users start with no `user_title_state`
rows. Anonymous/local profile state is not supported.

`user_watch_events`

Append-only per-user playback boundary events. These rows record what the app can
observe around the embedded player:

- `user_id`, `anime_id`, `episode_id`, `video_source_id`.
- `client_session_id`.
- `event_type`: `player_loaded`, `player_engaged`, `heartbeat`,
  `fullscreen_enter`, `pip_open`, `episode_selected`, `source_changed`,
  `page_hidden`, or `session_end`.
- Episode/source/provider labels, `engaged_seconds`, visibility/focus flags,
  confidence, and small JSON metadata.

`player_loaded` is only a technical signal. It does not update user progress by
itself.

`user_title_navigation_state`

Per-user navigation cursor for a canonical title. It stores the last opened
`episode_id`, its textual episode number as a resilient fallback, and
`updated_at`. Opening or selecting an episode updates this cursor without
changing library status, watch progress, or recommendation signals. Explicit
episode links and Continue Watching targets override the cursor for that entry
and then become the newly remembered episode.

`user_episode_state`

Aggregated per-user episode state derived from `user_watch_events`:

- Primary key `(user_id, anime_id, episode_id)`.
- Episode/source/provider labels for the latest known source.
- `first_seen_at`, `last_seen_at`, `started_at`, `completed_at`.
- `engaged_seconds`, `heartbeat_count`, `last_event_type`, and confidence
  fields.

Strong watch signals update `user_title_state.progress_episode_number`.
Episode/source selection signals remain telemetry only and do not start or
resume a title by themselves.

## Canonical Title View

The API presents a canonical catalog view over the source-specific `anime` rows.
When one AnimeGO row and one YummyAnime row have the same normalized title and
year, they are exposed as one catalog item with `source_variants`.

- AnimeGO is the primary variant when present. The visible numeric `id`, title,
  metadata, and default detail page come from that AnimeGO row.
- Each canonical title also exposes a local textual `slug`/`internal_id`, built
  from the title plus a short stable suffix. This is the shareable URL identity
  for the visible title and is separate from upstream/source numeric IDs.
- YummyAnime remains available as a source variant for source selection and
  player/source aggregation.
- Auto-merge is intentionally conservative: ambiguous buckets with more than
  one row per source are left as separate rows until they can be reviewed.
- User state is written to the canonical primary row for the current user. If
  state was previously attached to a duplicate variant, the API aggregates that
  user's rows and moves future updates to the primary row.

The current local recovery snapshot lives in `db/backups/current/` and includes
both the full SQLite database backup and user-state SQL/JSON/CSV exports. The
`db/` directory is intentionally ignored by git.

## ID Conventions

- AnimeGO title IDs use the upstream AnimeGO numeric ID.
- YummyAnime derived title IDs use `10_000_000 + source_id`.
- YummyAnime derived episode IDs use `anime_id * 1000 + episode_number`.
- YummyAnime/YummyAni translation IDs use reserved high ranges to avoid
  collisions with AnimeGO translation IDs.
- Canonical title URLs use local textual slugs such as
  `/reinkarnaciya-bezrabotnogo-3-sezon-5yc9`; numeric IDs remain internal for
  source-row joins and user-state storage.

## Mutable vs Scraped Data

Scraped data is refreshed by scraper runs and should be treated as replaceable.
User state is local and should not be overwritten by scraper imports.

The frontend currently relies on `source_count` and `available_episode_count`
computed by the API, not stored directly on `anime`. Recommendation scores,
ranks, reasons, and component scores are also computed API fields.

## Curated Franchise Layer

Franchises sit above canonical titles:

`source rows -> canonical release -> franchise`

Checked-in definitions live in `content/franchises/*.json`. They keep editorial
facts that cannot be derived safely from scraped titles: a franchise summary,
separate release and recommended-watch orders, release role/type, optionality,
watch notes, and source-backed official announcements. A definition can also
include a missing or future release with no playable catalog match.

Playable entries match by stable `(source, source_id)` keys, with AniDB or
Shikimori IDs as resilient fallbacks. The server resolves those source rows to
the existing canonical item before exposing the franchise API, so duplicate
AnimeGO/YummyAnime rows remain one release in the timeline. Similar titles are
never grouped automatically.

The curated franchise set is declared in `content/franchise-seeds.json`. It is
an expanding coverage catalog, not a fixed-size top list.
`scripts/generate_franchise_catalog.py` turns that manifest and Shikimori's
franchise metadata into checked-in definitions; production never depends on a
runtime request to Shikimori. The generator also follows prequel/sequel links
from the primary title because newly announced continuations can appear in the
relation graph before Shikimori assigns their franchise tag. Other graph links
are not unioned, and a graph continuation with a different non-empty franchise
tag fails generation for manual review, so crossovers cannot silently merge
unrelated brands.

Generated entries keep release order as the safe default because one tag may
contain several continuities; the manifest's editorial guide owns
branch-specific viewing advice. Hand-authored cards may still define a
separate recommended order. Regenerate all cards or one card with:

```bash
.venv/bin/python scripts/generate_franchise_catalog.py --write
.venv/bin/python scripts/generate_franchise_catalog.py --write --slug one-piece
```

After generation, verify local catalog coverage and one-to-one ownership of
canonical titles. The report also includes a rating-sorted editorial inbox of
popular uncovered titles so omissions remain visible. That inbox contains
individual titles and is intentionally not presented as an automatic franchise
detector:

```bash
.venv/bin/python scripts/check_franchise_data.py --db db/animego.sqlite
```

## External URLs

`url` and `cover_url` point to upstream sites. `embed_url` points to an external
player host and is needed for playback. `embed_url_redacted` exists so the app
can preserve useful host/provider identity without exposing full tokenized URLs
where a redacted value is enough for uniqueness and inspection.
