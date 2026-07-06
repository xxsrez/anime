#!/usr/bin/env python3
import argparse
import json
import sqlite3
import time
import traceback
import urllib.error

import scrape_animego as animego
import scrape_yummyanime as yummy


def connect(db_path):
    con = animego.init_db(db_path)
    con.row_factory = sqlite3.Row
    return con


def playable_missing_rows(con, source=None, limit=0, anime_ids=None):
    params = []
    where = [
        """
        not exists (
            select 1
            from video_sources vs
            where vs.anime_id = a.id
              and vs.embed_url is not null
        )
        """
    ]
    if source:
        where.append("a.source = ?")
        params.append(source)
    if anime_ids:
        where.append(f"a.id in ({','.join('?' for _ in anime_ids)})")
        params.extend(anime_ids)
    sql = f"""
        select *
        from anime a
        where {' and '.join(where)}
        order by
            case a.source when 'animego' then 0 when 'yummyanime' then 1 else 9 end,
            cast(a.year as integer) desc,
            a.id desc
    """
    if limit:
        sql += " limit ?"
        params.append(limit)
    return [dict(row) for row in con.execute(sql, params)]


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
            if exc.code not in {429, 500, 502, 503, 504} or attempt == attempts:
                raise
            wait = args.retry_backoff * attempt
            print(f"  retry {attempt}/{attempts} after HTTP {exc.code}, sleeping {wait:.1f}s")
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
    animego.upsert_anime(con, item_from_row(row, detail), detail, scraped_at)
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
    animego.upsert_anime(con, item, detail, scraped_at)
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


def backfill(args):
    con = connect(args.db)
    rows = playable_missing_rows(con, args.source, args.limit, args.anime_id)
    scraped_at = animego.now_iso()
    imported = 0
    episode_count = 0
    source_count = 0
    failed = 0

    for index, row in enumerate(rows, start=1):
        print(f"[{index}/{len(rows)}] {row['source']} {row['id']} {row['title']}")
        try:
            if row["source"] == "animego":
                episodes, sources, reason = backfill_animego(con, row, args, scraped_at)
            elif row["source"] == "yummyanime":
                episodes, sources, reason = backfill_yummyanime(con, row, args, scraped_at)
            else:
                reason = f"unsupported source: {row['source']}"
                episodes = sources = 0
            con.commit()
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
    print(f"backfilled {imported} titles, {episode_count} episodes, {source_count} provider rows, {failed} failed")


def main():
    parser = argparse.ArgumentParser(description="Backfill playable episode/provider rows for metadata-only imported titles.")
    parser.add_argument("--db", default="data/animego.sqlite")
    parser.add_argument("--source", choices=["animego", "yummyanime"])
    parser.add_argument("--anime-id", type=int, action="append", help="backfill one source row id; can be repeated")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--episode-limit", type=int, default=1, help="episodes per title to fetch; 0 means all")
    parser.add_argument("--delay", type=float, default=0.25)
    parser.add_argument("--retry-attempts", type=int, default=4)
    parser.add_argument("--retry-backoff", type=float, default=8.0)
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    backfill(args)


if __name__ == "__main__":
    main()
