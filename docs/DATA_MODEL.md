# Data Model

The project uses one SQLite database, `data/animego.sqlite`, for scraped catalog
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
- `watched`.
- `updated_at`.

The primary key is `(user_id, anime_id)`. The recommendation endpoint reads
this table for the current user as the taste profile. Favorites are strongest,
watched titles are weaker, and in-progress titles also count. Recommendations
themselves are computed at request time and are not persisted. New Google users
start with no `user_title_state` rows. Anonymous/local profile state is not
supported.

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

The current committed recovery snapshot lives in `backups/current/` and includes
both the full SQLite database backup and user-state SQL/JSON/CSV exports.

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

## External URLs

`url` and `cover_url` point to upstream sites. `embed_url` points to an external
player host and is needed for playback. `embed_url_redacted` exists so the app
can preserve useful host/provider identity without exposing full tokenized URLs
where a redacted value is enough for uniqueness and inspection.
