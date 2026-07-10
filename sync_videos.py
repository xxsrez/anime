#!/usr/bin/env python3
import argparse
from collections import OrderedDict, defaultdict
from contextlib import ExitStack
import copy
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
import urllib.error
from urllib.parse import urlencode
from urllib.request import Request

import backfill_players
import content_updates
import scrape_animego as animego
import scrape_yummyanime as yummy
from scripts import db_data_diff
from scripts.atomic_publish import atomic_publish_directory
from scripts.http_safety import open_validated_url
from scripts.operation_lock import DatabaseOperationLock, OperationLockError, default_lock_path


YUMMY_FEED_URL = f"{yummy.YUMMYANI_API_BASE}/feed"
YUMMY_ONGOING_PAGE_SIZE = 100
RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}
ROOT = Path(__file__).resolve().parent
DEFAULT_PRIVATE_MIGRATIONS = ROOT / "data" / "private-migrations"


class SyncFailedError(RuntimeError):
    def __init__(self, stats):
        self.stats = stats
        failed = sum(int(source_stats.get("failed") or 0) for source_stats in stats.values())
        super().__init__(f"sync completed with {failed} failed operation(s)")


class FileLock(DatabaseOperationLock):
    """Backward-compatible wrapper used by older callers and tests."""

    def __init__(self, path, wait=False, timeout=30.0):
        super().__init__("", path=path, wait=wait, timeout=timeout, operation="content sync")


def connect(db_path):
    con = animego.init_db(db_path)
    con.row_factory = sqlite3.Row
    return con


def ensure_sync_tables(con):
    con.executescript(
        """
        create table if not exists video_sync_runs (
            id integer primary key autoincrement,
            mode text not null,
            source text not null,
            started_at text not null,
            finished_at text not null,
            stats_json text not null
        );

        create table if not exists video_sync_state (
            key text primary key,
            value text,
            updated_at text not null
        );
        """
    )
    content_updates.ensure_schema(con)


def anime_row_exists(con, anime_id):
    return con.execute("select 1 from anime where id = ? limit 1", (anime_id,)).fetchone() is not None


def translation_known_for_episode(con, episode_id, translation_title):
    normalized = content_updates.normalize_key(translation_title)
    rows = con.execute(
        """
        select translation_title
        from video_sources
        where episode_id = ?
          and embed_url is not null
        """,
        (episode_id,),
    ).fetchall()
    return any(content_updates.normalize_key(row[0]) == normalized for row in rows)


def record_update_events(con, args, item, writes, *, new_title, reason):
    if args.dry_run:
        return 0
    run_id = getattr(args, "content_update_run_id", None)
    source = item.get("source")
    source_id = str(item.get("source_id", item.get("id")))
    event_count = 0

    if new_title:
        provider_count = sum(len(providers) for episode, providers, *_ in writes)
        translation_titles = sorted(
            {
                provider.get("translation_title")
                for episode, providers, *_ in writes
                for provider in providers
                if provider.get("translation_title")
            }
        )
        event_count += int(
            content_updates.insert_event(
                con,
                run_id,
                "new_title",
                item["id"],
                source=source,
                source_id=source_id,
                title=item.get("title"),
                description=f"Новый тайтл: {item.get('title')}",
                metadata={
                    "reason": reason,
                    "episode_count": len(writes),
                    "provider_count": provider_count,
                    "translations": translation_titles[:12],
                },
            )
        )
        return event_count

    for write in writes:
        episode = write[0]
        providers = write[1]
        before = write[-1] if isinstance(write[-1], dict) else {}
        episode_id = episode["id"]
        episode_number = episode.get("number")
        if not before.get("episode_had_playable"):
            event_count += int(
                content_updates.insert_event(
                    con,
                    run_id,
                    "new_episode",
                    item["id"],
                    episode_id=episode_id,
                    source=source,
                    source_id=source_id,
                    episode_number=episode_number,
                    title=item.get("title"),
                    description=f"Добавлена серия {episode_number or ''}".strip(),
                    metadata={
                        "reason": reason,
                        "provider_count": len(providers),
                    },
                )
            )
            continue

        seen_translation_events = set()
        for provider in providers:
            identity = modern_yummy_provider_identity(provider)
            if identity is not None and identity in before.get("known_provider_identities", set()):
                # A stable upstream provider whose URL/metadata rotated was
                # refreshed, not newly added content.
                continue
            translation_title = provider.get("translation_title")
            translation_key = content_updates.normalize_key(translation_title)
            if not before.get("known_translations", {}).get(translation_key):
                if translation_key in seen_translation_events:
                    continue
                seen_translation_events.add(translation_key)
                event_count += int(
                    content_updates.insert_event(
                        con,
                        run_id,
                        "new_translation",
                        item["id"],
                        episode_id=episode_id,
                        source=source,
                        source_id=source_id,
                        episode_number=episode_number,
                        translation_title=translation_title,
                        provider_title=provider.get("provider_title"),
                        title=item.get("title"),
                        description=f"Новая озвучка: {translation_title or 'без названия'}",
                        metadata={"reason": reason},
                        dedupe_key=content_updates.event_dedupe_key(
                            "new_translation",
                            item["id"],
                            episode_id,
                            translation_title=translation_title,
                        ),
                    )
                )
            else:
                event_count += int(
                    content_updates.insert_event(
                        con,
                        run_id,
                        "new_provider",
                        item["id"],
                        episode_id=episode_id,
                        source=source,
                        source_id=source_id,
                        episode_number=episode_number,
                        translation_title=translation_title,
                        provider_title=provider.get("provider_title"),
                        title=item.get("title"),
                        description=f"Новый плеер: {provider.get('provider_title') or 'без названия'}",
                        metadata={"reason": reason, "embed_host": provider.get("embed_host")},
                        dedupe_key=content_updates.event_dedupe_key(
                            "new_provider",
                            item["id"],
                            episode_id,
                            provider=provider,
                        ),
                    )
                )
    return event_count


def call_with_retries(args, label, func):
    attempts = max(1, args.retry_attempts)
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except urllib.error.HTTPError as exc:
            last_exc = exc
            code = exc.code
            exc.close()
            if code not in RETRYABLE_HTTP_CODES or attempt == attempts:
                raise
            wait = args.retry_backoff * attempt
            print(f"  retry {label} after HTTP {code} ({attempt}/{attempts}), sleeping {wait:.1f}s")
            time.sleep(wait)
        except urllib.error.URLError as exc:
            last_exc = exc
            if attempt == attempts:
                raise
            wait = args.retry_backoff * attempt
            print(f"  retry {label} after network error ({attempt}/{attempts}), sleeping {wait:.1f}s")
            time.sleep(wait)
    raise last_exc


def api_headers(args):
    headers = {
        "User-Agent": yummy.USER_AGENT,
        "Accept": "application/json",
    }
    if args.yummyani_token:
        headers["X-Application"] = args.yummyani_token
    return headers


def fetch_json_url(url, args):
    def fetch():
        if args.delay:
            time.sleep(args.delay)
        req = Request(url, headers=api_headers(args))
        with open_validated_url(req, timeout=30, allowed_hosts=("api.yani.tv",)) as response:
            return json.loads(response.read().decode("utf-8", "replace"))

    return call_with_retries(args, url, fetch)


def provider_has_embed(provider):
    return bool(provider.get("embed_url") and provider.get("embed_url_redacted"))


def modern_yummy_provider_identity(provider):
    provider_id = provider.get("provider_id")
    if not str(provider_id or "").startswith("yummyani-"):
        return None
    return (str(provider_id), int(provider.get("translation_id") or 0))


def modern_yummy_provider_identity_known(con, episode_id, provider):
    identity = modern_yummy_provider_identity(provider)
    if identity is None:
        return False
    row = con.execute(
        """
        select 1
        from video_sources
        where episode_id = ?
          and provider_id = ?
          and coalesce(translation_id, 0) = ?
          and embed_url is not null
        limit 1
        """,
        (episode_id, *identity),
    ).fetchone()
    return row is not None


def provider_known(con, episode_id, provider):
    identity = modern_yummy_provider_identity(provider)
    if identity is not None:
        rows = con.execute(
            """
            select provider_title, translation_title, embed_host, embed_url, embed_url_redacted
            from video_sources
            where episode_id = ?
              and provider_id = ?
              and coalesce(translation_id, 0) = ?
              and embed_url is not null
            """,
            (episode_id, *identity),
        ).fetchall()
        refreshed_fields = (
            "provider_title",
            "translation_title",
            "embed_host",
            "embed_url",
            "embed_url_redacted",
        )
        return any(
            all((row[field] or "") == (provider.get(field) or "") for field in refreshed_fields)
            for row in rows
        )
    row = con.execute(
        """
        select 1
        from video_sources
        where episode_id = ?
          and coalesce(provider_id, '') = coalesce(?, '')
          and coalesce(translation_id, 0) = coalesce(?, 0)
          and coalesce(embed_url_redacted, '') = coalesce(?, '')
          and embed_url is not null
        limit 1
        """,
        (
            episode_id,
            provider.get("provider_id"),
            provider.get("translation_id"),
            provider.get("embed_url_redacted"),
        ),
    ).fetchone()
    return row is not None


def provider_id_known(con, provider_id):
    if not provider_id:
        return False
    row = con.execute(
        "select 1 from video_sources where provider_id = ? and embed_url is not null limit 1",
        (provider_id,),
    ).fetchone()
    return row is not None


def episode_has_playable(con, episode_id):
    row = con.execute(
        "select 1 from video_sources where episode_id = ? and embed_url is not null limit 1",
        (episode_id,),
    ).fetchone()
    return row is not None


def selected_episodes(episodes, episode_limit):
    return animego.selected_episodes(episodes, episode_limit)


def providers_for_episode(providers, episode):
    episode_number = str(episode.get("number") or "")
    return [
        provider
        for provider in providers
        if provider_has_embed(provider) and provider.get("episode_number") in (None, episode_number)
    ]


def filter_episodes_with_providers(episodes, providers):
    playable = [provider for provider in providers if provider_has_embed(provider)]
    if not playable:
        return []
    if any(provider.get("episode_number") is None for provider in playable):
        return episodes
    numbers = {str(provider.get("episode_number")) for provider in playable}
    return [episode for episode in episodes if str(episode.get("number") or "") in numbers]


def quarantine_cross_episode_provider_urls(con, anime_id, writes, *, keep_empty=False):
    episodes_by_url = defaultdict(set)
    for row in con.execute(
        """
        select episode_id, embed_url
        from video_sources
        where anime_id = ?
          and embed_url is not null
          and trim(embed_url) <> ''
        """,
        (anime_id,),
    ).fetchall():
        episodes_by_url[row["embed_url"]].add(int(row["episode_id"]))
    for write in writes:
        episode_id = int(write[0]["id"])
        for provider in write[1]:
            embed_url = provider.get("embed_url")
            if embed_url:
                episodes_by_url[embed_url].add(episode_id)

    ambiguous_urls = {url for url, episode_ids in episodes_by_url.items() if len(episode_ids) > 1}
    if not ambiguous_urls:
        return writes, 0

    filtered_writes = []
    removed = 0
    for write in writes:
        providers = [provider for provider in write[1] if provider.get("embed_url") not in ambiguous_urls]
        removed += len(write[1]) - len(providers)
        if providers or keep_empty:
            filtered_writes.append((write[0], providers, *write[2:]))
    return filtered_writes, removed


def write_sync_run(con, mode, source, started_at, stats):
    finished_at = animego.now_iso()
    succeeded = not int(stats.get("failed") or 0)
    con.execute(
        """
        insert into video_sync_runs(mode, source, started_at, finished_at, stats_json)
        values (?, ?, ?, ?, ?)
        """,
        (mode, source, started_at, finished_at, json.dumps(dict(stats), ensure_ascii=False, sort_keys=True)),
    )
    for suffix, value in (
        ("last_attempt", finished_at),
        ("last_status", "success" if succeeded else "failed"),
    ):
        con.execute(
            """
            insert into video_sync_state(key, value, updated_at)
            values (?, ?, ?)
            on conflict(key) do update set value=excluded.value, updated_at=excluded.updated_at
            """,
            (f"{source}:{mode}:{suffix}", value, finished_at),
        )
    if succeeded:
        con.execute(
        """
        insert into video_sync_state(key, value, updated_at)
        values (?, ?, ?)
        on conflict(key) do update set value=excluded.value, updated_at=excluded.updated_at
        """,
        (f"{source}:{mode}:last_success", finished_at, finished_at),
        )


def finish_title(con, args):
    if args.dry_run:
        con.execute("rollback to sync_videos_dry_run")
        con.execute("release sync_videos_dry_run")
    else:
        con.commit()


def begin_title(con, args):
    if args.dry_run:
        con.execute("savepoint sync_videos_dry_run")


def abort_title(con, args):
    if args.dry_run:
        try:
            con.execute("rollback to sync_videos_dry_run")
            con.execute("release sync_videos_dry_run")
        except sqlite3.Error:
            con.rollback()
    else:
        con.rollback()


def yummy_page_url(anime_ref):
    value = str(anime_ref)
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return f"{yummy.YUMMYANI_BASE_URL}/catalog/item/{value}"


def selected_years(years, from_year, to_year):
    if years:
        return sorted(set(years), reverse=True)
    if from_year is None and to_year is None:
        return []
    if from_year is None or to_year is None:
        raise ValueError("--from-year and --to-year must be used together")
    if from_year > to_year:
        raise ValueError("--from-year must be less than or equal to --to-year")
    return list(range(to_year, from_year - 1, -1))


def parse_yummy_detail_for_sync(page_url, args):
    if yummy.is_modern_yummyani_url(page_url):
        return call_with_retries(
            args,
            page_url,
            lambda: yummy.parse_modern_detail(page_url, include_embed_urls=True, delay=args.delay),
        )

    html_text = call_with_retries(
        args,
        page_url,
        lambda: yummy.fetch_text(page_url, delay=args.delay),
    )
    return yummy.parse_detail(
        page_url,
        html_text,
        include_embed_urls=True,
        skip_player=False,
        delay=args.delay,
    )


def yummy_feed_provider_id(row):
    provider_title = yummy.modern_provider_title(row.get("player_title"))
    if yummy.should_skip_modern_provider(provider_title):
        return None
    video_id = row.get("video_id")
    if video_id is None:
        return None
    return f"yummyani-{provider_title.lower()}-{video_id}"


def sync_yummy_title(con, anime_ref, args, stats, reason):
    page_url = yummy_page_url(anime_ref)
    stats["titles_checked"] += 1
    item, detail, episodes, providers = parse_yummy_detail_for_sync(page_url, args)
    new_title = not anime_row_exists(con, item["id"])
    providers = [provider for provider in providers if provider_has_embed(provider)]
    selected = selected_episodes(episodes, args.episode_limit)
    selected = filter_episodes_with_providers(selected, providers) if not args.include_empty_episodes else selected
    if not selected and not args.include_empty_episodes:
        stats["metadata_only_skipped"] += 1
        return

    writes = []
    for episode in selected:
        episode_providers = providers_for_episode(providers, episode)
        known_provider_identities = {
            identity
            for provider in episode_providers
            if (identity := modern_yummy_provider_identity(provider)) is not None
            and modern_yummy_provider_identity_known(con, episode["id"], provider)
        }
        if not args.refresh_known:
            episode_providers = [
                provider
                for provider in episode_providers
                if not provider_known(con, episode["id"], provider)
            ]
        if episode_providers or args.include_empty_episodes:
            known_translations = {
                content_updates.normalize_key(provider.get("translation_title")): translation_known_for_episode(
                    con,
                    episode["id"],
                    provider.get("translation_title"),
                )
                for provider in episode_providers
            }
            writes.append(
                (
                    episode,
                    episode_providers,
                    {
                        "episode_had_playable": episode_has_playable(con, episode["id"]),
                        "known_translations": known_translations,
                        "known_provider_identities": known_provider_identities,
                    },
                )
            )

    if not writes:
        stats["known_skipped"] += 1
        return

    writes, duplicate_providers = quarantine_cross_episode_provider_urls(
        con,
        item["id"],
        writes,
        keep_empty=args.include_empty_episodes,
    )
    if duplicate_providers:
        stats["cross_episode_provider_urls_skipped"] += duplicate_providers
    if not writes:
        stats["known_skipped"] += 1
        return

    animego.upsert_anime(
        con,
        item,
        detail,
        args.scraped_at,
        authoritative_metadata=reason != "manual",
    )
    for episode, episode_providers, _before in writes:
        animego.upsert_episode(
            con,
            item["id"],
            episode,
            bool(episode_providers),
            None if episode_providers else "player not found",
            args.scraped_at,
        )
        stats["episodes_written"] += 1
        for provider in episode_providers:
            animego.upsert_provider(con, item["id"], episode["id"], provider, True, args.scraped_at)
            stats["providers_written"] += 1
    stats["update_events_written"] += record_update_events(con, args, item, writes, new_title=new_title, reason=reason)

    stats["titles_imported"] += 1
    print(f"  yummyanime {item['id']} {item['title']}: {len(writes)} episodes from {reason}")


def sync_yummy_feed(con, args, stats):
    try:
        payload = fetch_json_url(YUMMY_FEED_URL, args)
    except Exception as exc:
        stats["feed_failed"] += 1
        stats["failed"] += 1
        print(f"yummyanime feed: ERROR after retries: {exc}")
        if args.stop_on_error:
            raise
        return
    response = payload.get("response") or {}
    grouped = OrderedDict()
    for row in response.get("new_videos") or []:
        provider_id = yummy_feed_provider_id(row)
        if provider_id is None:
            stats["feed_alloha_skipped"] += 1
            continue
        if not args.refresh_known and provider_id_known(con, provider_id):
            stats["feed_known_skipped"] += 1
            continue
        anime_url = row.get("anime_url")
        if anime_url:
            grouped.setdefault(anime_url, 0)
            grouped[anime_url] += 1

    refs = list(grouped)
    if args.yummy_limit:
        refs = refs[: args.yummy_limit]
    print(f"yummyanime feed candidates: {len(refs)}")
    for index, anime_ref in enumerate(refs, start=1):
        print(f"[yummyanime feed {index}/{len(refs)}] {anime_ref}")
        begin_title(con, args)
        try:
            sync_yummy_title(con, anime_ref, args, stats, "feed")
            finish_title(con, args)
        except Exception as exc:
            abort_title(con, args)
            stats["failed"] += 1
            print(f"  ERROR: {exc}")
            if args.verbose:
                traceback.print_exc()
            if args.stop_on_error:
                raise


def sync_yummy_ongoing(con, args, stats):
    refs = []
    seen = set()
    offset = 0
    max_pages = max(1, int(getattr(args, "yummy_ongoing_max_pages", 100)))
    page_count = 0
    while page_count < max_pages:
        page_count += 1
        query = urlencode(
            {
                "status": "ongoing",
                "limit": YUMMY_ONGOING_PAGE_SIZE,
                "offset": offset,
                "sort": "id",
                "sort_forward": "false",
            }
        )
        try:
            payload = fetch_json_url(f"{yummy.YUMMYANI_API_BASE}/anime?{query}", args)
        except Exception as exc:
            stats["ongoing_failed"] += 1
            stats["failed"] += 1
            print(f"yummyanime ongoing offset {offset}: ERROR after retries: {exc}")
            if args.stop_on_error:
                raise
            break
        rows = payload.get("response") or []
        if not rows:
            break
        before_count = len(refs)
        for row in rows:
            anime_url = row.get("anime_url")
            if anime_url and anime_url not in seen:
                seen.add(anime_url)
                refs.append(anime_url)
        if len(rows) < YUMMY_ONGOING_PAGE_SIZE:
            break
        if args.yummy_limit and len(refs) >= args.yummy_limit:
            break
        if len(refs) == before_count:
            stats["ongoing_failed"] += 1
            stats["failed"] += 1
            print(
                f"yummyanime ongoing offset {offset}: full page contained no new titles; "
                "stopping to avoid an infinite pagination loop"
            )
            break
        offset += YUMMY_ONGOING_PAGE_SIZE
    else:
        stats["ongoing_failed"] += 1
        stats["failed"] += 1
        print(
            f"yummyanime ongoing: reached --yummy-ongoing-max-pages="
            f"{max_pages} before the feed ended"
        )

    if args.yummy_limit:
        refs = refs[: args.yummy_limit]
    print(f"yummyanime ongoing candidates: {len(refs)}")
    for index, anime_ref in enumerate(refs, start=1):
        print(f"[yummyanime ongoing {index}/{len(refs)}] {anime_ref}")
        begin_title(con, args)
        try:
            sync_yummy_title(con, anime_ref, args, stats, "ongoing")
            finish_title(con, args)
        except Exception as exc:
            abort_title(con, args)
            stats["failed"] += 1
            print(f"  ERROR: {exc}")
            if args.verbose:
                traceback.print_exc()
            if args.stop_on_error:
                raise


def sync_yummyanime(con, args):
    stats = defaultdict(int)
    for index, anime_ref in enumerate(args.yummy_refs or [], start=1):
        print(f"[yummyanime manual {index}/{len(args.yummy_refs)}] {anime_ref}")
        begin_title(con, args)
        try:
            sync_yummy_title(con, anime_ref, args, stats, "manual")
            finish_title(con, args)
        except Exception as exc:
            abort_title(con, args)
            stats["failed"] += 1
            print(f"  ERROR: {exc}")
            if args.verbose:
                traceback.print_exc()
            if args.stop_on_error:
                raise

    catalog_years = selected_years(
        args.yummy_catalog_years,
        args.yummy_from_year,
        args.yummy_to_year,
    )
    catalog_refs = []
    if catalog_years:
        catalog_refs = yummy.collect_catalog_urls(catalog_years, args.yummy_catalog_max_pages, delay=args.delay)
        if args.yummy_limit:
            catalog_refs = catalog_refs[: args.yummy_limit]
        print(f"yummyanime catalog candidates: {len(catalog_refs)}")

    for index, anime_ref in enumerate(catalog_refs, start=1):
        print(f"[yummyanime catalog {index}/{len(catalog_refs)}] {anime_ref}")
        begin_title(con, args)
        try:
            sync_yummy_title(con, anime_ref, args, stats, "catalog")
            finish_title(con, args)
        except Exception as exc:
            abort_title(con, args)
            stats["failed"] += 1
            print(f"  ERROR: {exc}")
            if args.verbose:
                traceback.print_exc()
            if args.stop_on_error:
                raise

    if args.mode == "manual":
        return stats

    sync_yummy_feed(con, args, stats)
    if args.mode in {"daily", "full"}:
        sync_yummy_ongoing(con, args, stats)
    return stats


def animego_item_from_row(row):
    return {
        "id": row["id"],
        "slug": row["slug"],
        "title": row["title"],
        "subtitle": row["subtitle"],
        "url": row["url"],
        "cover_url": row["cover_url"],
        "source": "animego",
        "source_id": row["source_id"] or str(row["id"]),
        "listing_score": row["listing_score"],
        "kind": row["kind"],
        "year": row["year"],
        "genres": [],
        "listing_description": row["description"],
    }


def animego_item_from_ref(anime_ref, con=None):
    url = animego.absolute_url(str(anime_ref))
    anime_id = animego.parse_anime_id(url)
    if not anime_id:
        raise ValueError(f"AnimeGO manual refs must be title URLs with numeric ids: {anime_ref}")
    slug = animego.parse_slug(url)
    item = {
        "id": anime_id,
        "slug": slug,
        "title": slug,
        "subtitle": None,
        "url": url,
        "cover_url": None,
        "source": "animego",
        "source_id": str(anime_id),
        "listing_score": None,
        "kind": None,
        "year": None,
        "genres": [],
        "listing_description": None,
    }
    if con is not None:
        cursor = con.execute("select * from anime where id = ?", (anime_id,))
        row = cursor.fetchone()
        if row is not None:
            if not hasattr(row, "keys"):
                row = dict(zip((column[0] for column in cursor.description), row, strict=True))
            item = animego_item_from_row(row)
            item["slug"] = slug or item["slug"]
            item["url"] = url
    return item


def collect_animego_listing(args, stats=None):
    years = selected_years(
        args.animego_season_years,
        args.animego_from_year,
        args.animego_to_year,
    )
    source_urls = [f"{animego.BASE_URL}/anime/season/{year}" for year in years]
    if not source_urls:
        source_urls = [args.animego_start_url]

    items = []
    seen = set()
    max_pages = args.animego_discover_pages
    if max_pages == 0:
        max_pages = args.animego_max_pages
    for source_url in source_urls:
        source_url = source_url.rstrip("/")
        for page in range(1, max_pages + 1):
            page_url = source_url if page == 1 else f"{source_url}/{page}"
            try:
                page_html = call_with_retries(
                    args,
                    page_url,
                    lambda page_url=page_url: animego.fetch_text(page_url, delay=args.delay),
                )
            except urllib.error.HTTPError as exc:
                if exc.code == 404 and args.animego_discover_pages == 0:
                    print(f"animego listing {source_url} page {page}: 404, stopping")
                    break
                if stats is not None:
                    stats["listing_failed"] += 1
                    stats["failed"] += 1
                print(f"animego listing {page_url}: ERROR after retries: {exc}")
                if args.stop_on_error:
                    raise
                break
            except urllib.error.URLError as exc:
                if stats is not None:
                    stats["listing_failed"] += 1
                    stats["failed"] += 1
                print(f"animego listing {page_url}: ERROR after retries: {exc}")
                if args.stop_on_error:
                    raise
                break
            page_items = animego.parse_listing(page_html)
            new_items = [item for item in page_items if item["id"] not in seen]
            for item in new_items:
                seen.add(item["id"])
                item["source"] = "animego"
                item["source_id"] = str(item["id"])
                items.append(item)
            print(f"animego listing {source_url} page {page}: {len(new_items)} new titles")
            if args.animego_discover_pages == 0 and not new_items:
                break
    return items


def animego_ongoing_rows(con):
    rows = con.execute(
        """
        select *
        from anime
        where source = 'animego'
        order by cast(coalesce(year, '0') as integer) desc, id desc
        """
    ).fetchall()
    return [dict(row) for row in rows if "онго" in str(row["status"] or "").casefold()]


def animego_source_unavailable_error(error):
    if isinstance(error, urllib.error.HTTPError):
        return error.code in RETRYABLE_HTTP_CODES
    return isinstance(error, urllib.error.URLError)


def sync_animego_item(con, item, args, stats, reason):
    stats["titles_checked"] += 1
    new_title = not anime_row_exists(con, item["id"])
    detail = call_with_retries(
        args,
        item["url"],
        lambda: animego.parse_detail(animego.fetch_text(item["url"], delay=args.delay)),
    )
    if not detail["player_url"]:
        stats["no_player_skipped"] += 1
        return

    player_json = call_with_retries(
        args,
        detail["player_url"],
        lambda: json.loads(animego.fetch_text(detail["player_url"], ajax=True, referer=item["url"], delay=args.delay)),
    )
    content = player_json.get("data", {}).get("content") or ""
    selected_episode_id, episodes, _, initial_providers = animego.parse_player_content(content)
    if not episodes and initial_providers:
        episodes = [animego.synthetic_episode(item["id"], item["title"])]

    writes = []
    for episode in selected_episodes(episodes, args.episode_limit):
        if args.missing_only and not args.refresh_known and episode_has_playable(con, episode["id"]):
            stats["episode_known_skipped"] += 1
            continue

        if initial_providers and (
            episode["id"] == int(item["id"]) * 1000 + 1
            or selected_episode_id == episode["id"]
            or len(episodes) == 1
        ):
            providers = initial_providers
            unavailable_reason = None
        else:
            episode_id = episode["id"]
            video_json = call_with_retries(
                args,
                f"/player/videos/{episode_id}",
                lambda episode_id=episode_id: json.loads(
                    animego.fetch_text(f"/player/videos/{episode_id}", ajax=True, referer=item["url"], delay=args.delay)
                ),
            )
            data = video_json.get("data", {})
            video_content = data.get("content") or ""
            _, _, _, providers = animego.parse_player_content(video_content)
            unavailable_reason = animego.parse_unavailable_reason(data.get("content_online") or "")

        providers = [provider for provider in providers if provider_has_embed(provider)]
        if not args.refresh_known:
            providers = [
                provider
                for provider in providers
                if not provider_known(con, episode["id"], provider)
            ]
        if providers or args.include_empty_episodes:
            known_translations = {
                content_updates.normalize_key(provider.get("translation_title")): translation_known_for_episode(
                    con,
                    episode["id"],
                    provider.get("translation_title"),
                )
                for provider in providers
            }
            writes.append(
                (
                    episode,
                    providers,
                    unavailable_reason,
                    {
                        "episode_had_playable": episode_has_playable(con, episode["id"]),
                        "known_translations": known_translations,
                    },
                )
            )
        else:
            stats["episode_without_new_provider_skipped"] += 1

    if not writes:
        stats["known_skipped"] += 1
        return

    writes, duplicate_providers = quarantine_cross_episode_provider_urls(
        con,
        item["id"],
        writes,
        keep_empty=args.include_empty_episodes,
    )
    if duplicate_providers:
        stats["cross_episode_provider_urls_skipped"] += duplicate_providers
    if not writes:
        stats["known_skipped"] += 1
        return

    animego.upsert_anime(
        con,
        item,
        detail,
        args.scraped_at,
        authoritative_metadata=reason != "manual",
    )
    for episode, providers, unavailable_reason, _before in writes:
        animego.upsert_episode(
            con,
            item["id"],
            episode,
            bool(providers),
            None if providers else unavailable_reason or "player not found",
            args.scraped_at,
        )
        stats["episodes_written"] += 1
        for provider in providers:
            animego.upsert_provider(con, item["id"], episode["id"], provider, True, args.scraped_at)
            stats["providers_written"] += 1
    stats["update_events_written"] += record_update_events(con, args, item, writes, new_title=new_title, reason=reason)

    stats["titles_imported"] += 1
    print(f"  animego {item['id']} {item['title']}: {len(writes)} episodes from {reason}")


def sync_animego(con, args):
    stats = defaultdict(int)
    candidates = OrderedDict()
    for index, anime_ref in enumerate(args.animego_refs or [], start=1):
        print(f"[animego manual {index}/{len(args.animego_refs)}] {anime_ref}")
        begin_title(con, args)
        try:
            sync_animego_item(con, animego_item_from_ref(anime_ref, con), args, stats, "manual")
            finish_title(con, args)
        except Exception as exc:
            abort_title(con, args)
            stats["failed"] += 1
            print(f"  ERROR: {exc}")
            if args.verbose:
                traceback.print_exc()
            if args.stop_on_error:
                raise

    should_collect_listing = args.mode != "manual" or selected_years(
        args.animego_season_years,
        args.animego_from_year,
        args.animego_to_year,
    )
    if should_collect_listing:
        for item in collect_animego_listing(args, stats):
            candidates[item["id"]] = (item, "listing")
    listing_unavailable = bool(stats.get("listing_failed"))

    if args.mode == "manual":
        items = list(candidates.values())
        if args.animego_limit:
            items = items[: args.animego_limit]
        print(f"animego candidates: {len(items)}")
        for index, (item, reason) in enumerate(items, start=1):
            print(f"[animego {index}/{len(items)}] {item['title']}")
            begin_title(con, args)
            try:
                sync_animego_item(con, item, args, stats, reason)
                finish_title(con, args)
            except Exception as exc:
                abort_title(con, args)
                stats["failed"] += 1
                print(f"  ERROR: {exc}")
                if args.verbose:
                    traceback.print_exc()
                if args.stop_on_error:
                    raise
        return stats

    for row in animego_ongoing_rows(con):
        candidates.setdefault(row["id"], (animego_item_from_row(row), "existing ongoing"))

    missing_rows = backfill_players.playable_missing_rows(
        con,
        source="animego",
        limit=args.animego_missing_limit,
    )
    for row in missing_rows:
        candidates.setdefault(row["id"], (animego_item_from_row(row), "missing coverage"))

    items = list(candidates.values())
    if args.animego_limit:
        items = items[: args.animego_limit]
    print(f"animego candidates: {len(items)}")

    for index, (item, reason) in enumerate(items, start=1):
        print(f"[animego {index}/{len(items)}] {item['title']}")
        begin_title(con, args)
        try:
            sync_animego_item(con, item, args, stats, reason)
            finish_title(con, args)
        except Exception as exc:
            abort_title(con, args)
            stats["failed"] += 1
            print(f"  ERROR: {exc}")
            if args.verbose:
                traceback.print_exc()
            if args.stop_on_error:
                raise
            if listing_unavailable and animego_source_unavailable_error(exc):
                skipped = len(items) - index
                stats["source_unavailable"] += 1
                stats["circuit_breaker_skipped"] += skipped
                print(
                    "animego source unavailable after listing and title failures; "
                    f"circuit breaker skipped {skipped} remaining candidates"
                )
                break
    return stats


def apply_mode_defaults(args):
    yummy_catalog_requested = bool(args.yummy_catalog_years or args.yummy_from_year or args.yummy_to_year)
    animego_catalog_requested = bool(args.animego_season_years or args.animego_from_year or args.animego_to_year)
    if not args.sources:
        if args.mode == "manual":
            args.sources = []
            if args.yummy_refs or yummy_catalog_requested:
                args.sources.append("yummyanime")
            if args.animego_refs or animego_catalog_requested:
                args.sources.append("animego")
        else:
            args.sources = ["yummyanime", "animego"]
    if args.mode == "manual" and not (
        args.yummy_refs
        or args.animego_refs
        or yummy_catalog_requested
        or animego_catalog_requested
    ):
        raise SystemExit("--mode manual requires at least one title ref or catalog year selector")
    if args.mode == "manual":
        if (args.yummy_refs or yummy_catalog_requested) and "yummyanime" not in args.sources:
            raise SystemExit("YummyAnime refs/catalog years require --source yummyanime in manual mode")
        if (args.animego_refs or animego_catalog_requested) and "animego" not in args.sources:
            raise SystemExit("AnimeGO refs/catalog years require --source animego in manual mode")
    if args.missing_only is None:
        args.missing_only = args.mode == "hourly"
    if args.animego_discover_pages is None:
        args.animego_discover_pages = 1 if args.mode == "hourly" else 3 if args.mode == "daily" else 0
    if args.animego_missing_limit is None:
        args.animego_missing_limit = 25 if args.mode == "hourly" else 0
    if args.animego_limit is None:
        args.animego_limit = 50 if args.mode == "hourly" else 0
    if args.yummy_limit is None:
        args.yummy_limit = 0
    args.scraped_at = animego.now_iso()
    return args


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Video-first periodic sync for Anime Local.")
    parser.add_argument("--db", default="db/animego.sqlite")
    parser.add_argument("--mode", choices=["manual", "hourly", "daily", "full"], default="hourly")
    parser.add_argument("--source", dest="sources", action="append", choices=["animego", "yummyanime"])
    parser.add_argument("--yummy-ref", dest="yummy_refs", action="append", default=[], help="YummyAni title URL or slug to sync explicitly")
    parser.add_argument("--animego-ref", dest="animego_refs", action="append", default=[], help="AnimeGO title URL to sync explicitly")
    parser.add_argument("--episode-limit", type=int, default=0, help="episodes per title to fetch; 0 means all")
    parser.add_argument("--refresh-known", action="store_true", help="also rewrite/update already known video source rows")
    parser.add_argument("--include-empty-episodes", action="store_true", help="allow episode rows without playable providers")
    parser.add_argument("--missing-only", dest="missing_only", action="store_true", help="skip episodes that already have playable video")
    parser.add_argument("--no-missing-only", dest="missing_only", action="store_false", help="check known episodes for newly added providers")
    parser.set_defaults(missing_only=None)
    parser.add_argument("--animego-start-url", default=animego.START_URL)
    parser.add_argument("--animego-season-year", dest="animego_season_years", action="append", type=int)
    parser.add_argument("--animego-from-year", type=int)
    parser.add_argument("--animego-to-year", type=int)
    parser.add_argument("--animego-discover-pages", type=int, help="AnimeGO listing pages; 0 means all until stop/max")
    parser.add_argument("--animego-max-pages", type=int, default=20)
    parser.add_argument("--animego-limit", type=int, help="limit AnimeGO title candidates; 0 means no limit")
    parser.add_argument("--animego-missing-limit", type=int, help="limit AnimeGO underfilled candidates; 0 means no limit")
    parser.add_argument("--yummy-limit", type=int, help="limit YummyAni title candidates; 0 means no limit")
    parser.add_argument("--yummy-catalog-year", dest="yummy_catalog_years", action="append", type=int)
    parser.add_argument("--yummy-from-year", type=int)
    parser.add_argument("--yummy-to-year", type=int)
    parser.add_argument("--yummy-catalog-max-pages", type=int, default=100)
    parser.add_argument(
        "--yummy-ongoing-max-pages",
        type=int,
        default=100,
        help="hard safety limit for ongoing-feed pagination",
    )
    parser.add_argument("--yummyani-token", default="", help="optional X-Application header value for api.yani.tv")
    parser.add_argument("--delay", type=float, default=0.25)
    parser.add_argument("--retry-attempts", type=int, default=4)
    parser.add_argument("--retry-backoff", type=float, default=8.0)
    parser.add_argument("--lock-file", help="override sync lock path")
    parser.add_argument("--wait-lock", action="store_true")
    parser.add_argument("--lock-timeout", type=float, default=30.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--trigger", default="manual", help="free-form run trigger label for content_update_runs")
    parser.add_argument(
        "--emit-migration",
        metavar="NAME",
        help="write the resulting catalog/player data patch instead of changing --db",
    )
    parser.add_argument(
        "--migrations-root",
        default=str(DEFAULT_PRIVATE_MIGRATIONS),
        help="output root for generated catalog/player SQL data patches",
    )
    parser.add_argument("--migration-script", default="00_data-update.sql")
    parser.add_argument("--migration-overwrite", action="store_true")
    parser.add_argument("--migration-max-bytes", type=int, default=900_000)
    return apply_mode_defaults(parser.parse_args(argv))


def copy_database(source_path, target_path):
    source = sqlite3.connect(source_path)
    target = sqlite3.connect(target_path)
    try:
        with target:
            source.backup(target)
    finally:
        target.close()
        source.close()


def migration_folder_name(value):
    raw = str(value).strip().replace(os.sep, "-")
    if os.altsep:
        raw = raw.replace(os.altsep, "-")
    safe = "".join(char if char.isalnum() or char in "-_." else "-" for char in raw).strip("-_.")
    if not safe:
        raise ValueError("--emit-migration must not be empty")
    if len(safe) >= 10 and safe[4:5] == "-" and safe[7:8] == "-":
        return safe
    return f"{animego.now_iso()[:10]}_{safe}"


def write_migration_file(root, folder, filename, sql, overwrite=False):
    if not sql.strip():
        return None
    if "/" in filename or (os.altsep and os.altsep in filename):
        raise ValueError("--migration-script must be a file name, not a path")
    if not filename.lower().endswith(".sql"):
        raise ValueError("--migration-script must end with .sql")
    path = Path(root) / folder / filename
    if path.exists() and not overwrite:
        raise FileExistsError(f"migration file already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sql, encoding="utf-8")
    return path


def chunked_migration_filename(filename, index):
    path = Path(filename)
    return f"{path.stem}-{index:02d}{path.suffix}"


def migration_bundle_members(folder_path, filename):
    base = Path(filename)
    chunk_pattern = f"{base.stem}-*{base.suffix}"
    members = []
    direct = folder_path / base.name
    if direct.exists():
        members.append(direct)
    members.extend(
        path
        for path in folder_path.glob(chunk_pattern)
        if path.is_file()
        and path.stem[len(base.stem) + 1 :].isdigit()
    )
    return sorted(set(members))


def write_migration_files(
    root,
    folder,
    filename,
    header,
    statements,
    overwrite=False,
    max_bytes=900_000,
    publish_wait=False,
    publish_timeout=30.0,
):
    if "/" in filename or (os.altsep and os.altsep in filename):
        raise ValueError("--migration-script must be a file name, not a path")
    if not filename.lower().endswith(".sql"):
        raise ValueError("--migration-script must end with .sql")

    if max_bytes is not None and max_bytes < 1:
        max_bytes = None

    iterator = iter(statements)
    try:
        first_statement = None
        for statement in iterator:
            if statement and statement.strip():
                first_statement = statement.rstrip()
                break
        if first_statement is None:
            return []

        root = Path(root)
        target_folder = root / folder
        root.mkdir(parents=True, exist_ok=True)
        with (
            atomic_publish_directory(
                target_folder,
                stage_prefix=f".{folder}.stage-",
                previous_prefix=f".{folder}.old-",
                wait=publish_wait,
                timeout=publish_timeout,
                operation=f"migration publication: {folder}",
            ) as stage,
            ExitStack() as handles,
        ):
            existing = (
                migration_bundle_members(target_folder, filename)
                if target_folder.exists()
                else []
            )
            if existing and not overwrite:
                raise FileExistsError(
                    "migration bundle already exists: "
                    + ", ".join(str(path) for path in existing)
                )
            if target_folder.exists():
                shutil.copytree(target_folder, stage, dirs_exist_ok=True)
            for path in migration_bundle_members(stage, filename):
                path.unlink()

            provisional = stage / f".{Path(filename).stem}.building"
            output_names = []
            chunk_index = 0
            chunked = False
            handle = handles.enter_context(provisional.open("w", encoding="utf-8", newline="\n"))
            handle.write(header)
            current_size = len(header.encode("utf-8"))
            current_has_statement = False

            def finish_chunk(output_name):
                nonlocal handle
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
                handle.close()
                os.replace(provisional, stage / output_name)
                output_names.append(output_name)

            def start_chunk():
                nonlocal handle, current_size, current_has_statement
                handle = handles.enter_context(
                    provisional.open("w", encoding="utf-8", newline="\n")
                )
                current_size = 0
                current_has_statement = False

            def append_statement(statement):
                nonlocal current_size, current_has_statement
                prefix = "\n\n" if current_has_statement else ""
                handle.write(prefix)
                handle.write(statement)
                current_size += len(prefix.encode("utf-8")) + len(statement.encode("utf-8"))
                current_has_statement = True

            pending_statement = first_statement
            while pending_statement is not None:
                prefix_size = 2 if current_has_statement else 0
                candidate_size = prefix_size + len(pending_statement.encode("utf-8")) + 1
                if (
                    current_has_statement
                    and max_bytes is not None
                    and current_size + candidate_size > max_bytes
                ):
                    finish_chunk(chunked_migration_filename(filename, chunk_index))
                    chunk_index += 1
                    chunked = True
                    start_chunk()
                append_statement(pending_statement)
                pending_statement = None
                for statement in iterator:
                    if statement and statement.strip():
                        pending_statement = statement.rstrip()
                        break

            final_size = current_size + 1
            final_name = (
                chunked_migration_filename(filename, chunk_index)
                if chunked or (max_bytes is not None and final_size > max_bytes)
                else filename
            )
            finish_chunk(final_name)
        return [target_folder / output_name for output_name in output_names]
    finally:
        close = getattr(iterator, "close", None)
        if close:
            close()


def run_sync(args):
    started_at = animego.now_iso()
    lock_path = args.lock_file or default_lock_path(args.db)
    all_stats = {}

    with FileLock(lock_path, wait=args.wait_lock, timeout=args.lock_timeout):
        con = connect(args.db)
        run_id = None
        try:
            if not args.dry_run:
                ensure_sync_tables(con)
                run_id = content_updates.create_run(
                    con,
                    args.mode,
                    getattr(args, "trigger", "manual"),
                    args.sources,
                    started_at=started_at,
                )
                args.content_update_run_id = run_id
                con.commit()

            for source in args.sources:
                print(f"== {source} {args.mode} sync ==")
                source_error = None
                try:
                    if source == "yummyanime":
                        stats = sync_yummyanime(con, args)
                    elif source == "animego":
                        stats = sync_animego(con, args)
                    else:
                        raise ValueError(f"unsupported source: {source}")
                except Exception as exc:
                    stats = defaultdict(int)
                    stats["failed"] += 1
                    stats["source_failed"] += 1
                    stats["error"] = str(exc)[:500]
                    source_error = exc
                    action = "stopping" if args.stop_on_error else "continuing"
                    print(f"== {source} {args.mode} sync failed after retries, {action}: {exc}")
                all_stats[source] = dict(stats)
                if not args.dry_run:
                    write_sync_run(con, args.mode, source, started_at, stats)
                    con.commit()
                if source_error is not None and args.stop_on_error:
                    raise SyncFailedError(all_stats) from source_error
            if args.dry_run:
                con.rollback()
            elif run_id:
                status = "partial" if any((stats.get("failed") or 0) for stats in all_stats.values()) else "success"
                content_updates.finish_run(con, run_id, started_at, status, all_stats)
                con.commit()
        except Exception as exc:
            if not args.dry_run and run_id:
                try:
                    content_updates.finish_run(con, run_id, started_at, "failed", all_stats, error=exc)
                    con.commit()
                except sqlite3.Error:
                    con.rollback()
            raise
        finally:
            con.close()

    print(json.dumps(all_stats, ensure_ascii=False, indent=2, sort_keys=True))
    if any(int(stats.get("failed") or 0) for stats in all_stats.values()):
        raise SyncFailedError(all_stats)
    return all_stats


def emit_update_migration(args):
    source_db = Path(args.db)
    if not source_db.exists():
        raise FileNotFoundError(f"database does not exist: {source_db}")
    if args.dry_run:
        raise ValueError("--emit-migration cannot be combined with --dry-run")

    folder = migration_folder_name(args.emit_migration)
    lock_path = args.lock_file or default_lock_path(args.db)
    paths = []
    with tempfile.TemporaryDirectory(prefix="anime-sync-migration-") as tmpdir:
        before_db = Path(tmpdir) / "before.sqlite"
        after_db = Path(tmpdir) / "after.sqlite"
        with FileLock(lock_path, wait=args.wait_lock, timeout=args.lock_timeout):
            copy_database(source_db, before_db)

        copy_database(before_db, after_db)
        sync_args = copy.copy(args)
        sync_args.db = str(after_db)
        sync_args.lock_file = str(after_db) + ".sync.lock"
        sync_args.wait_lock = False
        sync_args.emit_migration = None
        run_sync(sync_args)

        header = "\n".join(
            [
                "-- Generated by sync_videos.py --emit-migration.",
                f"-- Source database: {source_db}",
                f"-- Sync mode: {args.mode}",
                "",
            ]
        )
        paths = write_migration_files(
            args.migrations_root,
            folder,
            args.migration_script,
            header,
            db_data_diff.iter_data_migration_statements(before_db, after_db),
            overwrite=args.migration_overwrite,
            max_bytes=args.migration_max_bytes,
        )

    if not paths:
        print("migration: no catalog/player data changes detected")
        return None
    for path in paths:
        print(f"migration: {path}")
    return paths


def main(argv=None):
    args = parse_args(argv)
    try:
        if args.emit_migration:
            emit_update_migration(args)
        else:
            run_sync(args)
        return 0
    except (SyncFailedError, OperationLockError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
