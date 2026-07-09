#!/usr/bin/env python3
import argparse
import json
import re
import sqlite3
import time
import traceback
import urllib.error

import scrape_animego as animego
import scrape_yummyanime as yummy
from scripts.operation_lock import DatabaseOperationLock, default_lock_path


def connect(db_path):
    con = animego.init_db(db_path)
    con.row_factory = sqlite3.Row
    return con


def parsed_episode_total(value):
    match = re.search(r"\d+", str(value or ""))
    return int(match.group(0)) if match else None


def needs_player_backfill(row):
    available = int(row.get("available_episode_count") or 0)
    episode_count = int(row.get("episode_count") or 0)
    expected = parsed_episode_total(row.get("episodes_text"))
    if available == 0:
        return True
    if episode_count > available:
        return True
    return expected is not None and expected > available


def playable_missing_rows(con, source=None, limit=0, anime_ids=None):
    params = []
    where = []
    if source:
        where.append("a.source = ?")
        params.append(source)
    if anime_ids:
        where.append(f"a.id in ({','.join('?' for _ in anime_ids)})")
        params.extend(anime_ids)
    sql = f"""
        with
            episode_counts as (
                select anime_id, count(*) as episode_count
                from episodes
                group by anime_id
            ),
            available_episode_counts as (
                select e.anime_id, count(distinct e.id) as available_episode_count
                from episodes e
                join video_sources vs on vs.episode_id = e.id
                where vs.embed_url is not null
                group by e.anime_id
            ),
            source_counts as (
                select anime_id, count(*) as source_count
                from video_sources
                where embed_url is not null
                group by anime_id
            )
        select
            a.*,
            coalesce(ec.episode_count, 0) as episode_count,
            coalesce(aec.available_episode_count, 0) as available_episode_count,
            coalesce(sc.source_count, 0) as source_count
        from anime a
        left join episode_counts ec on ec.anime_id = a.id
        left join available_episode_counts aec on aec.anime_id = a.id
        left join source_counts sc on sc.anime_id = a.id
        {"where " + " and ".join(where) if where else ""}
        order by
            case a.source when 'animego' then 0 when 'yummyanime' then 1 else 9 end,
            cast(a.year as integer) desc,
            a.id desc
    """
    rows = [dict(row) for row in con.execute(sql, params)]
    if not anime_ids:
        rows = [row for row in rows if needs_player_backfill(row)]
    if limit:
        rows = rows[:limit]
    return rows


def item_from_row(row, detail):
    return {
        "id": row["id"],
        "slug": row["slug"],
        "title": row["title"],
        "subtitle": row["subtitle"],
        "url": row["url"],
        "cover_url": row["cover_url"],
        "source": row["source"],
        "source_id": row["source_id"],
        "listing_score": row["listing_score"],
        "kind": row["kind"],
        "year": row["year"],
        "genres": detail.get("genres") or [],
        "listing_description": row["description"],
    }


def selected_episodes(episodes, episode_limit):
    if episode_limit == 0:
        return episodes
    return episodes[:episode_limit]


def fetch_with_retries(fetcher, args, *fetch_args, **fetch_kwargs):
    attempts = max(1, args.retry_attempts)
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return fetcher(*fetch_args, **fetch_kwargs)
        except urllib.error.HTTPError as exc:
            last_exc = exc
            code = exc.code
            exc.close()
            if code not in {429, 500, 502, 503, 504} or attempt == attempts:
                raise
            wait = args.retry_backoff * attempt
            print(f"  retry {attempt}/{attempts} after HTTP {code}, sleeping {wait:.1f}s")
            time.sleep(wait)
        except urllib.error.URLError as exc:
            last_exc = exc
            if attempt == attempts:
                raise
            wait = args.retry_backoff * attempt
            print(f"  retry {attempt}/{attempts} after network error, sleeping {wait:.1f}s")
            time.sleep(wait)
    raise last_exc


def backfill_animego(con, row, args, scraped_at):
    detail = animego.parse_detail(fetch_with_retries(animego.fetch_text, args, row["url"], delay=args.delay))
    animego.upsert_anime(
        con,
        item_from_row(row, detail),
        detail,
        scraped_at,
        authoritative_metadata=True,
    )
    if not detail["player_url"]:
        return 0, 0, "no player url"

    player_json = json.loads(fetch_with_retries(animego.fetch_text, args, detail["player_url"], ajax=True, referer=row["url"], delay=args.delay))
    content = player_json.get("data", {}).get("content") or ""
    selected_episode_id, episodes, _, initial_providers = animego.parse_player_content(content)
    if not episodes and initial_providers:
        episodes = [animego.synthetic_episode(row["id"], row["title"])]

    episode_count = 0
    source_count = 0
    for episode in selected_episodes(episodes, args.episode_limit):
        if initial_providers and (
            episode["id"] == int(row["id"]) * 1000 + 1
            or selected_episode_id == episode["id"]
            or len(episodes) == 1
        ):
            providers = initial_providers
            unavailable_reason = None
            has_video = True
        else:
            video_json = json.loads(fetch_with_retries(animego.fetch_text, args, f"/player/videos/{episode['id']}", ajax=True, referer=row["url"], delay=args.delay))
            data = video_json.get("data", {})
            video_content = data.get("content") or ""
            _, _, _, providers = animego.parse_player_content(video_content)
            unavailable_reason = animego.parse_unavailable_reason(data.get("content_online") or "")
            has_video = bool(data.get("numVideos", 0) or providers)
        providers = [
            provider
            for provider in providers
            if provider.get("embed_url") and provider.get("embed_url_redacted")
        ]
        has_video = bool(providers)
        animego.upsert_episode(con, row["id"], episode, has_video, unavailable_reason, scraped_at)
        episode_count += 1
        for provider in providers:
            animego.upsert_provider(con, row["id"], episode["id"], provider, True, scraped_at)
            source_count += 1
    return episode_count, source_count, None


def backfill_yummyanime(con, row, args, scraped_at):
    html_text = fetch_with_retries(yummy.fetch_text, args, row["url"], delay=args.delay)
    item, detail, episodes, providers = yummy.parse_detail(
        row["url"],
        html_text,
        include_embed_urls=True,
        skip_player=False,
        delay=args.delay,
    )
    animego.upsert_anime(con, item, detail, scraped_at, authoritative_metadata=True)
    providers = [
        provider
        for provider in providers
        if provider.get("embed_url") and provider.get("embed_url_redacted")
    ]
    if not providers:
        return 0, 0, "no providers"

    episode_count = 0
    source_count = 0
    for episode in selected_episodes(episodes, args.episode_limit):
        episode_providers = [
            provider
            for provider in providers
            if provider.get("episode_number") in (None, str(episode.get("number")))
        ]
        animego.upsert_episode(
            con,
            item["id"],
            episode,
            bool(episode_providers),
            None if episode_providers else "player not found",
            scraped_at,
        )
        episode_count += 1
        for provider in episode_providers:
            animego.upsert_provider(con, item["id"], episode["id"], provider, True, scraped_at)
            source_count += 1
    return episode_count, source_count, None


def _backfill(args):
    con = connect(args.db)
    rows = playable_missing_rows(con, args.source, args.limit, args.anime_id)
    scraped_at = animego.now_iso()
    imported = 0
    episode_count = 0
    source_count = 0
    failed = 0
    unresolved = 0

    for index, row in enumerate(rows, start=1):
        print(f"[{index}/{len(rows)}] {row['source']} {row['id']} {row['title']}")
        try:
            if row["source"] == "animego":
                episodes, sources, reason = backfill_animego(con, row, args, scraped_at)
            elif row["source"] == "yummyanime":
                episodes, sources, reason = backfill_yummyanime(con, row, args, scraped_at)
            else:
                raise ValueError(f"unsupported source: {row['source']}")
            con.commit()
            if reason:
                unresolved += 1
            else:
                imported += 1
            episode_count += episodes
            source_count += sources
            suffix = f", {reason}" if reason else ""
            print(f"  {episodes} episodes, {sources} provider rows{suffix}")
        except Exception as exc:
            con.rollback()
            failed += 1
            print(f"  ERROR: {exc}")
            if args.verbose:
                traceback.print_exc()
            if args.stop_on_error:
                break

    con.execute(
        """
        insert into scrape_runs(start_url, created_at, anime_count, episode_count, video_source_count, include_embed_urls)
        values (?, ?, ?, ?, ?, ?)
        """,
        (
            f"backfill:{args.source or 'all'}",
            scraped_at,
            imported,
            episode_count,
            source_count,
            1,
        ),
    )
    con.commit()
    con.close()
    print(
        f"backfilled {imported} titles, {episode_count} episodes, {source_count} provider rows, "
        f"{unresolved} unresolved, {failed} failed"
    )
    return {
        "imported": imported,
        "episodes": episode_count,
        "providers": source_count,
        "unresolved": unresolved,
        "failed": failed,
    }


def backfill(args):
    lock_path = getattr(args, "lock_file", None) or default_lock_path(args.db)
    with DatabaseOperationLock(
        args.db,
        path=lock_path,
        wait=getattr(args, "wait_lock", False),
        timeout=getattr(args, "lock_timeout", 30.0),
        operation="player backfill",
    ):
        return _backfill(args)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Backfill playable episode/provider rows for metadata-only imported titles.")
    parser.add_argument("--db", default="db/animego.sqlite")
    parser.add_argument("--source", choices=["animego", "yummyanime"])
    parser.add_argument("--anime-id", type=int, action="append", help="backfill one source row id; can be repeated")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--episode-limit", type=int, default=0, help="episodes per title to fetch; 0 means all")
    parser.add_argument("--delay", type=float, default=0.25)
    parser.add_argument("--retry-attempts", type=int, default=4)
    parser.add_argument("--retry-backoff", type=float, default=8.0)
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--lock-file")
    parser.add_argument("--wait-lock", action="store_true")
    parser.add_argument("--lock-timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    result = backfill(args)
    return 1 if result["failed"] or result["unresolved"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
