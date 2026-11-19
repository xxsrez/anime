#!/usr/bin/env python3
"""Refresh catalog ratings from the public MyAnimeList catalog via Tenrai."""

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mal_ratings  # noqa: E402
import server  # noqa: E402
from scripts import enrich_title_aliases as aliases  # noqa: E402
from scripts.generate_franchise_catalog import ShikimoriClient  # noqa: E402
from scripts.http_safety import open_validated_url  # noqa: E402
from scripts.operation_lock import DatabaseOperationLock  # noqa: E402


TENRAI_URL = "https://api.tenrai.org/v1/anime"
BATCH_SIZE = 25
REQUEST_INTERVAL_SECONDS = 0.6
SHIKIMORI_BATCH_SIZE = 8
KIND_BY_CATALOG_LABEL = {
    "фильм": {"movie"},
    "полнометражный фильм": {"movie"},
    "ова": {"ova"},
    "ova": {"ova"},
    "ona": {"ona"},
    "спешл": {"special", "tv_special"},
}


def load_catalog_rows(con):
    return {
        row["id"]: dict(row)
        for row in con.execute("""
            select id, title, subtitle, year, release_text, kind, status, episodes_text, source, source_id
            from anime order by id
        """)
    }


def release_year(row):
    return aliases.parse_year(row.get("year")) or aliases.parse_year(row.get("release_text"))


def verified_ids(rows, explicit, path):
    ids = explicit.copy()
    manual = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manual, dict):
        raise ValueError("Verified MAL IDs must be a JSON object")
    for row in rows:
        source_key = f'{row["source"]}:{row["source_id"]}'
        if source_key not in manual:
            continue
        value = manual[source_key]
        if value is None and row["id"] not in ids:
            ids[row["id"]] = set()
            continue
        mal_id = mal_ratings.positive_id(value)
        if mal_id is None or ids.get(row["id"], {mal_id}) != {mal_id}:
            raise ValueError(f"Invalid or conflicting MAL ID for {source_key}")
        ids[row["id"]] = {mal_id}
    return ids


def disambiguate_episodes(row, candidates):
    if (row.get("status") or "").casefold() not in {"вышел", "завершён", "завершен", "released"}:
        return candidates
    count = mal_ratings.positive_id(row.get("episodes_text"))
    matching = {mal_id for mal_id, episodes in candidates.items() if count and count == episodes}
    return {mal_id: candidates[mal_id] for mal_id in matching} if len(matching) == 1 else candidates


def match_catalog(rows, explicit_ids, entries):
    catalog = defaultdict(set)
    for row in rows:
        if row["id"] in explicit_ids:
            continue
        year = release_year(row)
        for label in (row["title"], row["subtitle"]):
            if year and (key := aliases.normalize(label)):
                catalog[year, key].add(row["id"])

    candidates = defaultdict(dict)
    for entry in entries:
        year = (entry.get("animeSeason") or {}).get("year")
        mal_id = aliases.aod_entry_ids(entry)["mal_id"]
        if year and mal_id:
            for key, _ in aliases.aod_match_keys(entry):
                for anime_id in catalog.get((year, key), ()):
                    candidates[anime_id][mal_id] = entry.get("episodes")

    assignments = {}
    stats = Counter()
    for row in rows:
        anime_id = row["id"]
        identifiers = explicit_ids.get(anime_id) or disambiguate_episodes(row, candidates[anime_id])
        if len(identifiers) == 1:
            assignments[anime_id] = next(iter(identifiers))
            stats["explicit_id" if anime_id in explicit_ids else "matched_title_and_year"] += 1
        elif identifiers:
            stats["ambiguous"] += 1
        else:
            stats["no_match"] += 1
    return assignments, stats


def shikimori_matches(rows, client):
    """Require exact title evidence and release metadata before accepting a MAL ID."""
    matches = {}
    for offset in range(0, len(rows), SHIKIMORI_BATCH_SIZE):
        batch = rows[offset : offset + SHIKIMORI_BATCH_SIZE]
        searches = [
            f'title{i}: animes(search:{json.dumps(row["title"], ensure_ascii=False)}, limit: 10) '
            "{ malId russian name english synonyms kind episodes airedOn { year date } }"
            for i, row in enumerate(batch)
        ]
        result = client.graphql("query { " + " ".join(searches) + " }")
        for i, row in enumerate(batch):
            key = aliases.normalize(row["title"])
            year = release_year(row)
            kinds = KIND_BY_CATALOG_LABEL.get(aliases.normalize(row.get("kind")))
            candidates = {}
            for entry in result[f"title{i}"]:
                mal_id = mal_ratings.positive_id(entry.get("malId"))
                date = entry.get("airedOn") or {}
                names = (entry["russian"], entry["name"], entry["english"], *entry["synonyms"])
                if not mal_id or not year or not any(aliases.normalize(name) == key for name in names):
                    continue
                if date.get("year") != year or (kinds and entry["kind"] not in kinds):
                    continue
                candidates[mal_id] = entry["episodes"]
            candidates = disambiguate_episodes(row, candidates)
            if len(candidates) == 1:
                matches[row["id"]] = next(iter(candidates))
    return matches


def fetch_batch(ids):
    query = urlencode({"ids": ",".join(map(str, ids)), "limit": BATCH_SIZE, "page": 1})
    request = Request(f"{TENRAI_URL}?{query}", headers={"User-Agent": "AnimeLocal/1.0"})
    for attempt in range(3):
        try:
            with open_validated_url(request, timeout=20, allowed_hosts=("api.tenrai.org",)) as response:
                payload = json.load(response)
            if payload["pagination"]["has_next_page"]:
                raise ValueError("MAL rating batch was unexpectedly paginated")
            return payload["data"]
        except HTTPError as error:
            retry_after = error.headers.get("Retry-After", "")
            error.close()
            if attempt == 2 or error.code not in {429, 500, 502, 503, 504}:
                raise
            delay = int(retry_after) if retry_after.isdecimal() else 2 ** (attempt + 1)
            if delay > 60:
                raise
            time.sleep(max(1, delay))
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2 ** (attempt + 1))
    raise AssertionError("unreachable")


def fetch_ratings(ids):
    ids = sorted(set(ids))
    ratings = {}
    for offset in range(0, len(ids), BATCH_SIZE):
        batch = ids[offset : offset + BATCH_SIZE]
        response = fetch_batch(batch)
        for entry in response:
            mal_id = mal_ratings.positive_id(entry["mal_id"])
            if mal_id not in batch or mal_id in ratings:
                raise ValueError(f"Unexpected MyAnimeList ID in response: {mal_id}")
            score = entry["score"]
            if score not in (None, 0) and mal_ratings.score_number(score) is None:
                raise ValueError(f"Invalid MyAnimeList score for ID {mal_id}")
            ratings[mal_id] = (mal_ratings.score_number(score), mal_ratings.positive_id(entry.get("scored_by")))
        if offset + BATCH_SIZE < len(ids):
            time.sleep(REQUEST_INTERVAL_SECONDS)
    return ratings


def apply_ratings(path, rows, explicit, assignments, ratings, checked_at, excluded):
    with closing(server.connect(path)) as con, DatabaseOperationLock(path, wait=True, timeout=30), con:
        con.execute("begin immediate")
        current_rows = load_catalog_rows(con)
        current_explicit = mal_ratings.explicit_ids(con)
        unchanged = {
            anime_id for anime_id, row in rows.items()
            if current_rows.get(anime_id) == row and current_explicit.get(anime_id) == explicit.get(anime_id)
        }
        records = [
            (anime_id, mal_id, *ratings[mal_id], checked_at)
            for anime_id, mal_id in assignments.items()
            if mal_id in ratings
            and anime_id in unchanged
        ]
        result = con.executemany(
            """
            insert into anime_mal_ratings(anime_id, mal_id, score, scored_by, checked_at)
            values (?, ?, ?, ?, ?)
            on conflict(anime_id) do update set
                mal_id=excluded.mal_id, score=excluded.score,
                scored_by=excluded.scored_by, checked_at=excluded.checked_at
            where anime_mal_ratings.checked_at <= excluded.checked_at
            """,
            records,
        )
        cleared = con.executemany(
            "delete from anime_mal_ratings where anime_id=? and checked_at <= ?",
            [(anime_id, checked_at) for anime_id in excluded & unchanged],
        )
    return result.rowcount, cleared.rowcount


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--aod", required=True, type=Path, help="Path to anime-offline-database.jsonl")
    parser.add_argument("--apply", action="store_true", help="Write verified ratings to the catalog")
    parser.add_argument("--missing-only", action="store_true", help="Skip rows already checked for a MAL rating")
    parser.add_argument("--verified-ids", type=Path, help="JSON object mapping verified source:source_id to MAL ID")
    args = parser.parse_args()
    path = args.db.resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as con:
        con.row_factory = sqlite3.Row
        rows = load_catalog_rows(con)
        explicit = mal_ratings.explicit_ids(con)
        known_ids = verified_ids(rows.values(), explicit, args.verified_ids) if args.verified_ids else explicit
        if args.missing_only and con.execute(
            "select 1 from sqlite_master where type='table' and name='anime_mal_ratings'"
        ).fetchone():
            checked = {
                anime_id
                for anime_id, mal_id in con.execute("select anime_id,mal_id from anime_mal_ratings")
                if known_ids.get(anime_id, {mal_id}) == {mal_id}
            }
            rows = {row_id: row for row_id, row in rows.items() if row_id not in checked}

    entries = (entry for _, entry in aliases.aod_entries(args.aod))
    assignments, stats = match_catalog(rows.values(), known_ids, entries)
    print(json.dumps({"catalog_rows": len(rows), "unique_mal_ids": len(set(assignments.values())), **stats}), flush=True)
    remaining = [
        row for row_id, row in rows.items()
        if row_id not in assignments and row_id not in known_ids
        and row["source"] in {"animego", "yummyanime"} and release_year(row)
    ]
    additional = shikimori_matches(remaining, ShikimoriClient("AnimeLocal/1.0", request_interval=1.0))
    assignments.update(additional)
    print(json.dumps({"matched_via_shikimori": len(additional), "rows_without_verified_mal_id": len(rows) - len(assignments)}), flush=True)
    checked_at = server.now_iso()
    ratings = fetch_ratings(assignments.values())
    scored = sum(mal_id in ratings and ratings[mal_id][0] is not None for mal_id in assignments.values())
    unrated = sum(mal_id in ratings and ratings[mal_id][0] is None for mal_id in assignments.values())
    excluded = {anime_id for anime_id, identifiers in known_ids.items() if not identifiers}
    written, cleared = (
        apply_ratings(path, rows, explicit, assignments, ratings, checked_at, excluded)
        if args.apply else (0, 0)
    )
    print(json.dumps({
        "scored": scored,
        "unrated": unrated,
        "id_not_returned": len(assignments) - scored - unrated,
        "written": written,
        "cleared_bundle_ratings": cleared,
    }))


if __name__ == "__main__":
    main()
