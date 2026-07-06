#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import math
import mimetypes
import os
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "animego.sqlite"
STATIC_DIR = ROOT / "static"
DEFAULT_RECOMMENDATION_LIMIT = 20
MAX_RECOMMENDATION_LIMIT = 50
SOURCE_PRIORITY = {
    "animego": 0,
    "yummyanime": 1,
}
MERGEABLE_SOURCES = {"animego", "yummyanime"}
CATALOG_CACHE = {}
CATALOG_CACHE_LOCK = threading.RLock()
SLUG_TRANSLIT = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "j",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "c",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def ensure_user_state_schema(con):
    exists = con.execute(
        "select 1 from sqlite_master where type = 'table' and name = 'user_title_state'"
    ).fetchone()
    if exists:
        return False
    con.execute(
        """
        create table user_title_state (
            anime_id integer primary key,
            is_favorite integer not null default 0,
            progress_episode_number integer,
            watched integer not null default 0,
            updated_at text not null,
            foreign key (anime_id) references anime(id) on delete cascade
        )
        """
    )
    return True


def ensure_columns(con, table, columns):
    existing = {row[1] for row in con.execute(f"pragma table_info({table})")}
    changed = False
    for column, definition in columns.items():
        if column not in existing:
            con.execute(f"alter table {table} add column {column} {definition}")
            changed = True
    return changed


def ensure_index(con, name, sql):
    exists = con.execute(
        "select 1 from sqlite_master where type = 'index' and name = ?",
        (name,),
    ).fetchone()
    if exists:
        return False
    con.execute(sql)
    return True


def ensure_catalog_schema(con):
    changed = ensure_columns(
        con,
        "anime",
        {
            "source": "text",
            "source_id": "text",
        },
    )
    if con.execute("select 1 from anime where source is null limit 1").fetchone():
        con.execute("update anime set source = 'animego' where source is null")
        changed = True
    if con.execute("select 1 from anime where source_id is null limit 1").fetchone():
        con.execute("update anime set source_id = cast(id as text) where source_id is null")
        changed = True
    return changed


def ensure_runtime_indexes(con):
    changed = False
    changed |= ensure_index(
        con,
        "idx_episodes_anime_id",
        "create index idx_episodes_anime_id on episodes(anime_id)",
    )
    changed |= ensure_index(
        con,
        "idx_video_sources_anime_embed",
        "create index idx_video_sources_anime_embed on video_sources(anime_id, embed_url)",
    )
    changed |= ensure_index(
        con,
        "idx_video_sources_episode_embed",
        "create index idx_video_sources_episode_embed on video_sources(episode_id, embed_url)",
    )
    return changed


def resolve_db_path(db_path=None):
    return Path(db_path or os.environ.get("ANIMEGO_DB") or DEFAULT_DB)


def db_signature(path):
    stat = path.stat()
    return (stat.st_mtime_ns, stat.st_size)


def connect(db_path=None):
    path = resolve_db_path(db_path)
    con = sqlite3.connect(path)
    con.execute("pragma busy_timeout=30000")
    con.row_factory = sqlite3.Row
    changed = ensure_catalog_schema(con)
    changed |= ensure_user_state_schema(con)
    changed |= ensure_runtime_indexes(con)
    if changed:
        con.commit()
    return con


def rows_to_dicts(rows):
    return [dict(row) for row in rows]


def normalize_state(row=None):
    if row is None:
        return {
            "is_favorite": False,
            "progress_episode_number": None,
            "watched": False,
            "updated_at": None,
        }
    return {
        "is_favorite": bool(row["is_favorite"]),
        "progress_episode_number": row["progress_episode_number"],
        "watched": bool(row["watched"]),
        "updated_at": row["updated_at"],
    }


def apply_state_fields(item):
    item["is_favorite"] = bool(item.get("is_favorite") or 0)
    item["watched"] = bool(item.get("watched") or 0)
    item["progress_episode_number"] = item.get("progress_episode_number")
    item["state_updated_at"] = item.get("state_updated_at")
    return item


def numeric(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def clamp(value, low=0.0, high=1.0):
    return max(low, min(high, value))


def normalize_key(value):
    return str(value or "").strip().casefold().replace("ё", "е").replace("э", "е")


def normalize_match_title(value):
    text = normalize_key(value)
    text = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def base36(value):
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    number = int(value or 0)
    if number == 0:
        return "0"
    result = []
    while number:
        number, remainder = divmod(number, 36)
        result.append(digits[remainder])
    return "".join(reversed(result))


def slugify_text(value, max_length=72):
    result = []
    previous_dash = False
    for char in str(value or "").casefold():
        if char.isascii() and char.isalnum():
            result.append(char)
            previous_dash = False
            continue
        mapped = SLUG_TRANSLIT.get(char)
        if mapped is not None:
            if mapped:
                result.append(mapped)
                previous_dash = False
            continue
        if not previous_dash:
            result.append("-")
            previous_dash = True
    slug = re.sub(r"-+", "-", "".join(result)).strip("-")
    if len(slug) > max_length:
        slug = slug[:max_length].rstrip("-")
    return slug or "anime"


def canonical_slug_for_item(item):
    base = slugify_text(item.get("title") or item.get("subtitle") or "anime")
    return f"{base}-{base36(item.get('id'))}"


def best_score(item):
    aggregate = numeric(item.get("aggregate_score"))
    if aggregate is not None:
        return aggregate
    return numeric(item.get("listing_score"))


def year_number(item):
    value = numeric(item.get("year"))
    if value is not None:
        return int(value)
    published = str(item.get("date_published") or "")
    if len(published) >= 4 and published[:4].isdigit():
        return int(published[:4])
    return None


def source_priority(source):
    return SOURCE_PRIORITY.get(source or "", 99)


def canonical_match_key(item):
    if item.get("source") not in MERGEABLE_SOURCES:
        return None
    title_key = normalize_match_title(item.get("title"))
    year = year_number(item)
    if not title_key or year is None:
        return None
    return (year, title_key)


def variant_from_item(item):
    return {
        "id": item["id"],
        "source": item.get("source"),
        "source_id": item.get("source_id"),
        "title": item.get("title"),
        "subtitle": item.get("subtitle"),
        "url": item.get("url"),
        "year": item.get("year"),
        "source_count": item.get("source_count") or 0,
        "available_episode_count": item.get("available_episode_count") or 0,
    }


def source_sort_key(item):
    return (
        source_priority(item.get("source")),
        0 if (numeric(item.get("source_count")) or 0) > 0 else 1,
        -(best_score(item) or 0),
        str(item.get("title") or ""),
        item.get("id") or 0,
    )


def unique_values(items, getter):
    values = []
    seen = set()
    for item in items:
        raw_values = getter(item)
        if not isinstance(raw_values, list):
            raw_values = [raw_values]
        for value in raw_values:
            key = normalize_key(value)
            if not key or key in seen:
                continue
            seen.add(key)
            values.append(value)
    return values


def aggregate_item_state(item, variants):
    item["is_favorite"] = any(bool(variant.get("is_favorite")) for variant in variants)
    item["watched"] = any(bool(variant.get("watched")) for variant in variants)
    progress_values = [
        variant.get("progress_episode_number")
        for variant in variants
        if variant.get("progress_episode_number") is not None
    ]
    item["progress_episode_number"] = max(progress_values) if progress_values else None
    item["state_updated_at"] = max(
        (variant.get("state_updated_at") for variant in variants if variant.get("state_updated_at")),
        default=None,
    )
    return item


def merge_canonical_items(items):
    sorted_items = sorted(items, key=source_sort_key)
    primary = sorted_items[0]
    merged = dict(primary)
    variants = [variant_from_item(item) for item in sorted_items]
    sources = unique_values(sorted_items, lambda item: item.get("source"))

    merged["id"] = primary["id"]
    merged["source"] = primary.get("source")
    merged["source_id"] = primary.get("source_id")
    merged["source_variants"] = variants
    merged["source_variant_count"] = len(variants)
    merged["sources"] = sources
    merged["source_member_ids"] = [variant["id"] for variant in variants]
    merged["source_count"] = sum((item.get("source_count") or 0) for item in sorted_items)
    merged["available_episode_count"] = max((item.get("available_episode_count") or 0) for item in sorted_items)
    merged["episode_count"] = max((item.get("episode_count") or 0) for item in sorted_items)
    merged["genres"] = unique_values(sorted_items, lambda item: item.get("genres") or [])

    for field in ("subtitle", "cover_url", "listing_score", "aggregate_score", "aggregate_count", "kind", "year", "status", "episodes_text"):
        if merged.get(field) in (None, ""):
            merged[field] = next((item.get(field) for item in sorted_items if item.get(field) not in (None, "")), merged.get(field))

    return aggregate_item_state(merged, sorted_items)


def canonicalize_items(items):
    buckets = {}
    groups = []
    for item in items:
        key = canonical_match_key(item)
        if key is None:
            groups.append(merge_canonical_items([item]))
            continue
        buckets.setdefault(key, []).append(item)

    for bucket in buckets.values():
        source_counts = {}
        for item in bucket:
            source_counts[item.get("source")] = source_counts.get(item.get("source"), 0) + 1
        title_key = normalize_match_title(bucket[0].get("title"))
        subtitle_keys = {normalize_match_title(item.get("subtitle")) for item in bucket if normalize_match_title(item.get("subtitle"))}
        short_title_has_matching_subtitle = len(title_key) >= 8 or len(subtitle_keys) == 1
        if len(source_counts) > 1 and all(count == 1 for count in source_counts.values()) and short_title_has_matching_subtitle:
            groups.append(merge_canonical_items(bucket))
        else:
            groups.extend(merge_canonical_items([item]) for item in bucket)

    for group in groups:
        slug = canonical_slug_for_item(group)
        group["slug"] = slug
        group["internal_id"] = slug
    groups.sort(key=lambda item: ((numeric(item.get("source_count")) or 0) <= 0, -(item.get("id") or 0)))
    return groups


def item_matches_query(item, query):
    if not query:
        return True
    variant_text = []
    for variant in item.get("source_variants") or []:
        variant_text.extend([variant.get("title"), variant.get("subtitle"), variant.get("source")])
    haystack = normalize_key(
        " ".join(
            str(part)
            for part in [
                item.get("title"),
                item.get("subtitle"),
                item.get("kind"),
                item.get("status"),
                item.get("year"),
                item.get("source"),
                *item.get("sources", []),
                *item.get("genres", []),
                *variant_text,
            ]
            if part
        )
    )
    return normalize_key(query) in haystack


def format_number(value):
    number = numeric(value)
    if number is None:
        return ""
    return f"{number:.1f}".rstrip("0").rstrip(".")


def recommendation_confidence(score):
    if score >= 76:
        return "высокая"
    if score >= 60:
        return "средняя"
    return "осторожная"


def normalize_recommendation_limit(limit):
    try:
        requested = int(limit)
    except (TypeError, ValueError):
        requested = DEFAULT_RECOMMENDATION_LIMIT
    return max(1, min(MAX_RECOMMENDATION_LIMIT, requested))


def seed_weight(item):
    if item.get("is_favorite"):
        return 2.0
    if item.get("watched"):
        return 1.1
    if item.get("progress_episode_number") is not None:
        return 0.8
    return 0.0


def item_genre_keys(item):
    if "_genre_keys" not in item:
        keys = set()
        for genre in item.get("genres", []):
            key = normalize_key(genre)
            if key:
                keys.add(key)
        item["_genre_keys"] = keys
    return item["_genre_keys"]


def item_genre_label_map(item):
    if "_genre_label_map" not in item:
        labels = {}
        for genre in item.get("genres", []):
            key = normalize_key(genre)
            if key:
                labels.setdefault(key, genre)
        item["_genre_label_map"] = labels
    return item["_genre_label_map"]


def public_item_copy(item):
    return {key: value for key, value in item.items() if not key.startswith("_")}


def build_recommendation_profile(items):
    seeds = [item for item in items if seed_weight(item) > 0]
    genre_weights = {}
    genre_labels = {}
    kind_weights = {}

    for item in seeds:
        weight = seed_weight(item)
        label_map = item_genre_label_map(item)
        for key in item_genre_keys(item):
            genre_weights[key] = genre_weights.get(key, 0.0) + weight
            genre_labels.setdefault(key, label_map[key])
        kind = item.get("kind")
        if kind:
            kind_weights[kind] = kind_weights.get(kind, 0.0) + weight

    top_genres = sorted(
        (
            {"genre": genre_labels[key], "weight": round(weight, 2)}
            for key, weight in genre_weights.items()
        ),
        key=lambda item: (-item["weight"], item["genre"]),
    )[:8]

    return {
        "seeds": seeds,
        "favorite_count": sum(1 for item in items if item.get("is_favorite")),
        "seed_count": len(seeds),
        "genre_weights": genre_weights,
        "genre_weight_desc": sorted(genre_weights.values(), reverse=True),
        "kind_weights": kind_weights,
        "top_genres": top_genres,
    }


def genre_profile_score(candidate, profile):
    candidate_keys = item_genre_keys(candidate)
    if not candidate_keys or not profile["genre_weights"]:
        return 0.0, []

    matched_weight = sum(profile["genre_weights"].get(key, 0.0) for key in candidate_keys)
    comparison_size = max(3, len(candidate_keys))
    top_possible = sum(profile["genre_weight_desc"][:comparison_size])
    score = clamp(matched_weight / top_possible) if top_possible else 0.0
    label_map = item_genre_label_map(candidate)
    matched = [
        label
        for key, label in label_map.items()
        if key in profile["genre_weights"]
    ]
    return score, matched


def seed_similarity(candidate, profile):
    candidate_keys = item_genre_keys(candidate)
    if not candidate_keys:
        return 0.0, []

    matches = []
    for seed in profile["seeds"]:
        seed_keys = item_genre_keys(seed)
        if not seed_keys:
            continue
        overlap = candidate_keys & seed_keys
        if not overlap:
            continue
        union = candidate_keys | seed_keys
        score = len(overlap) / len(union)
        if candidate.get("kind") and candidate.get("kind") == seed.get("kind"):
            score += 0.05
        label_map = item_genre_label_map(candidate)
        matches.append(
            {
                "id": seed["id"],
                "title": seed["title"],
                "score": round(clamp(score), 3),
                "matched_genres": [
                    label
                    for key, label in label_map.items()
                    if key in overlap
                ],
            }
        )

    matches.sort(key=lambda item: item["score"], reverse=True)
    best = matches[0]["score"] if matches else 0.0
    return best, matches[:2]


def quality_score(item):
    score = best_score(item)
    if score is None:
        return 0.35
    count = numeric(item.get("aggregate_count")) or 0
    prior = 7.2
    min_count = 50
    bayesian = ((score * count) + (prior * min_count)) / (count + min_count) if count else score
    return clamp((bayesian - 6.2) / 3.2)


def popularity_score(item):
    count = numeric(item.get("aggregate_count")) or 0
    return clamp(math.log10(count + 1) / 4.0) if count > 0 else 0.0


def has_playable_source(item):
    return (numeric(item.get("source_count")) or 0) > 0


def availability_score(item):
    available = numeric(item.get("available_episode_count")) or 0
    sources = numeric(item.get("source_count")) or 0
    if sources > 0:
        return clamp(0.75 + min(0.25, available / 48.0))
    if (numeric(item.get("episode_count")) or 0) > 0:
        return 0.25
    return 0.0


def recency_score(item):
    year = year_number(item)
    if year is None:
        return 0.35
    current_year = dt.datetime.now().year
    if year >= current_year:
        return 1.0
    if year == current_year - 1:
        return 0.85
    if year == current_year - 2:
        return 0.7
    if year >= current_year - 5:
        return 0.55
    return 0.4


def kind_profile_score(item, profile):
    kind = item.get("kind")
    if not kind or not profile["kind_weights"]:
        return 0.0
    max_weight = max(profile["kind_weights"].values())
    return clamp(profile["kind_weights"].get(kind, 0.0) / max_weight) if max_weight else 0.0


def watchable_recommendation_score(raw_score, item, has_watchable_candidates):
    score = clamp(raw_score) * 100
    if not has_watchable_candidates:
        return round(score, 1)
    if has_playable_source(item):
        return round(55 + (score * 0.45), 1)
    return round(score * 0.55, 1)


def recommendation_reasons(item, matched_genres, based_on):
    reasons = []
    if matched_genres:
        reasons.append(f"Совпали жанры: {', '.join(matched_genres[:4])}")
    if based_on:
        titles = ", ".join(match["title"] for match in based_on[:2])
        reasons.append(f"Близко к избранному: {titles}")

    score = best_score(item)
    if score is not None:
        rating = format_number(score)
        count = int(numeric(item.get("aggregate_count")) or 0)
        suffix = f" ({count} оценок)" if count >= 10 else ""
        reasons.append(f"Рейтинг {rating}/10{suffix}")

    available = int(numeric(item.get("available_episode_count")) or 0)
    if available:
        reasons.append(f"Есть видео: {available} сер.")
    elif item.get("source_count"):
        reasons.append("Есть доступные источники")
    else:
        reasons.append("Пока без видео в базе")

    if not reasons:
        reasons.append("Хороший общий рейтинг для стартовой рекомендации")
    return reasons[:4]


def get_recommendations(db_path=None, limit=DEFAULT_RECOMMENDATION_LIMIT):
    limit = normalize_recommendation_limit(limit or DEFAULT_RECOMMENDATION_LIMIT)
    items = get_anime_list(db_path)
    profile = build_recommendation_profile(items)
    has_profile = profile["seed_count"] > 0
    candidate_items = [
        item
        for item in items
        if not item.get("is_favorite")
        and not item.get("watched")
        and item.get("progress_episode_number") is None
    ]
    watchable_candidate_count = sum(1 for item in candidate_items if has_playable_source(item))
    has_watchable_candidates = watchable_candidate_count > 0
    recommendations = []

    for item in candidate_items:
        genre_score, matched_genres = genre_profile_score(item, profile)
        seed_score, based_on = seed_similarity(item, profile)
        taste_score = (0.7 * genre_score) + (0.3 * seed_score)
        quality = quality_score(item)
        popularity = popularity_score(item)
        availability = availability_score(item)
        recency = recency_score(item)
        kind_score = kind_profile_score(item, profile)

        if has_profile:
            raw_score = (
                (0.50 * taste_score)
                + (0.20 * quality)
                + (0.13 * availability)
                + (0.08 * popularity)
                + (0.05 * recency)
                + (0.04 * kind_score)
            )
        else:
            raw_score = (
                (0.36 * quality)
                + (0.30 * availability)
                + (0.18 * popularity)
                + (0.12 * recency)
                + (0.04 * kind_score)
            )

        score = watchable_recommendation_score(raw_score, item, has_watchable_candidates)
        item = public_item_copy(item)
        item["recommendation_score"] = score
        item["recommendation_confidence"] = recommendation_confidence(score)
        item["recommendation_matched_genres"] = matched_genres[:6]
        item["recommendation_based_on"] = based_on
        item["recommendation_reasons"] = recommendation_reasons(item, matched_genres, based_on)
        item["recommendation_components"] = {
            "taste": round(taste_score, 3),
            "quality": round(quality, 3),
            "availability": round(availability, 3),
            "popularity": round(popularity, 3),
            "recency": round(recency, 3),
            "watchable": 1.0 if has_playable_source(item) else 0.0,
            "raw": round(clamp(raw_score), 3),
        }
        recommendations.append(item)

    recommendations.sort(
        key=lambda item: (
            -item["recommendation_score"],
            -(numeric(item.get("source_count")) or 0),
            -(best_score(item) or 0),
            item["title"],
        )
    )
    recommendations = recommendations[:limit]
    for index, item in enumerate(recommendations, start=1):
        item["recommendation_rank"] = index

    return {
        "items": recommendations,
        "limit": limit,
        "profile": {
            "favorite_count": profile["favorite_count"],
            "seed_count": profile["seed_count"],
            "candidate_count": len(candidate_items),
            "watchable_candidate_count": watchable_candidate_count,
            "top_genres": profile["top_genres"],
            "mode": "personalized" if has_profile else "popular",
        },
    }


def get_source_anime_items(con):
    rows = con.execute(
        """
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
            ),
            genre_lists as (
                select anime_id, group_concat(genre) as genres
                from anime_genres
                group by anime_id
            )
        select
            a.id,
            a.title,
            a.subtitle,
            a.url,
            a.cover_url,
            a.source,
            a.source_id,
            a.listing_score,
            a.aggregate_score,
            a.aggregate_count,
            a.date_published,
            a.kind,
            a.year,
            a.status,
            a.episodes_text,
            coalesce(us.is_favorite, 0) as is_favorite,
            us.progress_episode_number,
            coalesce(us.watched, 0) as watched,
            us.updated_at as state_updated_at,
            coalesce(ec.episode_count, 0) as episode_count,
            coalesce(aec.available_episode_count, 0) as available_episode_count,
            coalesce(sc.source_count, 0) as source_count,
            gl.genres
        from anime a
        left join user_title_state us on us.anime_id = a.id
        left join episode_counts ec on ec.anime_id = a.id
        left join available_episode_counts aec on aec.anime_id = a.id
        left join source_counts sc on sc.anime_id = a.id
        left join genre_lists gl on gl.anime_id = a.id
        order by source_count > 0 desc, a.id desc
        """,
    ).fetchall()

    items = rows_to_dicts(rows)
    for item in items:
        apply_state_fields(item)
        item["genres"] = [g for g in (item.pop("genres") or "").split(",") if g]
        item["available_episode_count"] = item["available_episode_count"] or 0
    return items


def get_anime_list(db_path=None, q=None):
    items = clone_catalog_items(get_catalog_items(db_path))
    return [item for item in items if item_matches_query(item, q)]


def sql_placeholders(values):
    return ",".join("?" for _ in values)


def clone_catalog_item(item):
    cloned = dict(item)
    cloned["genres"] = list(item.get("genres") or [])
    cloned["sources"] = list(item.get("sources") or [])
    cloned["source_member_ids"] = list(item.get("source_member_ids") or [])
    cloned["source_variants"] = [dict(variant) for variant in item.get("source_variants") or []]
    return cloned


def clone_catalog_items(items):
    return [clone_catalog_item(item) for item in items]


def build_catalog_cache(db_path=None):
    path = resolve_db_path(db_path)
    con = connect(path)
    try:
        items = canonicalize_items(get_source_anime_items(con))
    finally:
        con.close()

    id_map = {}
    slug_map = {}
    for item in items:
        slug_map[item["slug"]] = item
        slug_map[item["internal_id"]] = item
        for variant in item.get("source_variants") or []:
            id_map[int(variant["id"])] = item

    return {
        "signature": db_signature(path),
        "items": items,
        "id_map": id_map,
        "slug_map": slug_map,
    }


def get_catalog_cache(db_path=None):
    path = resolve_db_path(db_path)
    key = str(path.resolve())
    signature = db_signature(path)
    with CATALOG_CACHE_LOCK:
        cached = CATALOG_CACHE.get(key)
        if cached and cached.get("signature") == signature:
            return cached
        cached = build_catalog_cache(path)
        CATALOG_CACHE[key] = cached
        return cached


def invalidate_catalog_cache(db_path=None):
    path = resolve_db_path(db_path)
    key = str(path.resolve())
    with CATALOG_CACHE_LOCK:
        CATALOG_CACHE.pop(key, None)


def get_catalog_items(db_path=None):
    return get_catalog_cache(db_path)["items"]


def canonical_group_for_anime_id(con, anime_id):
    db_path = con.execute("pragma database_list").fetchone()["file"]
    return get_catalog_cache(db_path)["id_map"].get(int(anime_id))


def canonical_group_for_anime_ref(con, anime_ref):
    value = str(anime_ref or "").strip()
    if not value:
        return None
    if value.isdigit():
        return canonical_group_for_anime_id(con, int(value))
    db_path = con.execute("pragma database_list").fetchone()["file"]
    return get_catalog_cache(db_path)["slug_map"].get(value)


def aggregate_state_rows(rows):
    if not rows:
        return normalize_state(None)
    progress_values = [
        row["progress_episode_number"]
        for row in rows
        if row["progress_episode_number"] is not None
    ]
    return {
        "is_favorite": any(bool(row["is_favorite"]) for row in rows),
        "progress_episode_number": max(progress_values) if progress_values else None,
        "watched": any(bool(row["watched"]) for row in rows),
        "updated_at": max((row["updated_at"] for row in rows if row["updated_at"]), default=None),
    }


def get_group_state(con, anime_ids):
    if not anime_ids:
        return normalize_state(None)
    rows = con.execute(
        f"select * from user_title_state where anime_id in ({sql_placeholders(anime_ids)})",
        anime_ids,
    ).fetchall()
    return aggregate_state_rows(rows)


def episode_number_key(value, fallback):
    raw = str(value or "").strip()
    number = numeric(raw)
    if number is not None:
        return f"n:{int(number)}" if float(number).is_integer() else f"n:{number}"
    key = normalize_key(raw)
    return f"s:{key}" if key else f"id:{fallback}"


def episode_key(episode):
    return episode_number_key(episode.get("number"), episode.get("id"))


def episode_sort_key(episode):
    number = numeric(episode.get("number"))
    return (
        number is None,
        number if number is not None else normalize_key(episode.get("number")),
        source_priority(episode.get("anime_source")),
        episode.get("id") or 0,
    )


def source_row_sort_key(source):
    return (
        source.get("episode_id") or 0,
        source_priority(source.get("source")),
        source.get("translation_title") or "",
        0 if normalize_key(source.get("provider_title")) == "kodik" else 1,
        source.get("provider_title") or "",
    )


def get_anime_detail(anime_ref, db_path=None):
    con = connect(db_path)
    group = canonical_group_for_anime_ref(con, anime_ref)
    if not group:
        con.close()
        return None

    member_ids = [variant["id"] for variant in group.get("source_variants") or []]
    primary_id = group["id"]
    member_sql = sql_placeholders(member_ids)

    anime = con.execute("select * from anime where id = ?", (primary_id,)).fetchone()
    if not anime:
        con.close()
        return None

    genres = rows_to_dicts(
        con.execute(
            f"""
            select distinct g.genre
            from anime_genres g
            join anime a on a.id = g.anime_id
            where g.anime_id in ({member_sql})
            order by case a.source when 'animego' then 0 when 'yummyanime' then 1 else 9 end, g.genre
            """,
            member_ids,
        ).fetchall()
    )
    dubbings = rows_to_dicts(
        con.execute(
            f"""
            select distinct d.dubbing
            from anime_dubbings d
            join anime a on a.id = d.anime_id
            where d.anime_id in ({member_sql})
            order by case a.source when 'animego' then 0 when 'yummyanime' then 1 else 9 end, d.dubbing
            """,
            member_ids,
        ).fetchall()
    )
    episode_rows = rows_to_dicts(
        con.execute(
            f"""
            select
                e.*,
                a.source as anime_source,
                a.source_id as anime_source_id,
                count(vs.id) as source_count
            from episodes e
            join anime a on a.id = e.anime_id
            left join video_sources vs on vs.episode_id = e.id and vs.embed_url is not null
            where e.anime_id in ({member_sql})
            group by e.id
            order by cast(e.number as integer), e.id
            """,
            member_ids,
        ).fetchall()
    )
    source_rows = rows_to_dicts(
        con.execute(
            f"""
            select
                vs.id,
                vs.anime_id as source_anime_id,
                vs.episode_id,
                vs.provider_id,
                vs.provider_title,
                vs.translation_id,
                vs.translation_title,
                vs.embed_host,
                vs.embed_url,
                vs.embed_url_redacted,
                a.source,
                a.source_id,
                e.number as episode_number
            from video_sources vs
            join anime a on a.id = vs.anime_id
            join episodes e on e.id = vs.episode_id
            where vs.anime_id in ({member_sql})
              and vs.embed_url is not null
            order by
                cast(e.number as integer),
                vs.episode_id,
                case a.source when 'animego' then 0 when 'yummyanime' then 1 else 9 end,
                vs.translation_title,
                case when lower(vs.provider_title) = 'kodik' then 0 else 1 end,
                vs.provider_title
            """,
            member_ids,
        ).fetchall()
    )
    fields = rows_to_dicts(
        con.execute(
            "select label, value from anime_fields where anime_id = ? order by label",
            (primary_id,),
        ).fetchall()
    )
    state = get_group_state(con, member_ids)
    con.close()

    episode_buckets = {}
    for episode in episode_rows:
        episode_buckets.setdefault(episode_key(episode), []).append(episode)

    episodes = []
    episode_id_by_key = {}
    for key, bucket in sorted(episode_buckets.items(), key=lambda item: episode_sort_key(sorted(item[1], key=episode_sort_key)[0])):
        selected = sorted(
            bucket,
            key=lambda episode: (
                0 if episode.get("anime_id") == primary_id else 1,
                source_priority(episode.get("anime_source")),
                0 if (episode.get("source_count") or 0) > 0 else 1,
                episode.get("id") or 0,
            ),
        )[0]
        episode = dict(selected)
        episode["source_count"] = 0
        episode.pop("anime_source", None)
        episode.pop("anime_source_id", None)
        episodes.append(episode)
        episode_id_by_key[key] = episode["id"]

    by_episode = {}
    for source in source_rows:
        key = episode_number_key(source.get("episode_number"), source.get("episode_id"))
        canonical_episode_id = episode_id_by_key.get(key)
        if canonical_episode_id is None:
            continue
        source = dict(source)
        source["episode_id"] = canonical_episode_id
        source.pop("episode_number", None)
        by_episode.setdefault(canonical_episode_id, []).append(source)

    for episode in episodes:
        episode["source_count"] = len(by_episode.get(episode["id"], []))
    for sources in by_episode.values():
        sources.sort(key=source_row_sort_key)

    detail = dict(anime)
    detail.update(state)
    detail["genres"] = [row["genre"] for row in genres]
    detail["dubbings"] = [row["dubbing"] for row in dubbings]
    detail["fields"] = fields
    detail["episodes"] = episodes
    detail["sources_by_episode"] = by_episode
    detail["source_variants"] = group.get("source_variants") or []
    detail["source_variant_count"] = group.get("source_variant_count") or 1
    detail["sources"] = group.get("sources") or [detail.get("source")]
    detail["source_member_ids"] = member_ids
    detail["slug"] = group.get("slug")
    detail["internal_id"] = group.get("internal_id")
    detail["source_count"] = sum(len(sources) for sources in by_episode.values())
    detail["available_episode_count"] = sum(1 for episode in episodes if episode.get("source_count"))
    return detail


def update_user_state(anime_ref, patch, db_path=None):
    con = connect(db_path)
    group = canonical_group_for_anime_ref(con, anime_ref)
    if not group:
        con.close()
        return None

    target_id = group["id"]
    member_ids = [variant["id"] for variant in group.get("source_variants") or []]
    current = get_group_state(con, member_ids)
    next_state = dict(current)

    if "is_favorite" in patch:
        next_state["is_favorite"] = bool(patch["is_favorite"])
    if "watched" in patch:
        next_state["watched"] = bool(patch["watched"])
    if "progress_episode_number" in patch:
        raw_value = patch["progress_episode_number"]
        if raw_value in (None, ""):
            next_state["progress_episode_number"] = None
        else:
            try:
                next_state["progress_episode_number"] = max(0, int(raw_value))
            except (TypeError, ValueError):
                con.close()
                raise ValueError("progress_episode_number must be a non-negative integer")

    next_state["updated_at"] = now_iso()
    con.execute(
        """
        insert into user_title_state (
            anime_id,
            is_favorite,
            progress_episode_number,
            watched,
            updated_at
        )
        values (?, ?, ?, ?, ?)
        on conflict(anime_id) do update set
            is_favorite = excluded.is_favorite,
            progress_episode_number = excluded.progress_episode_number,
            watched = excluded.watched,
            updated_at = excluded.updated_at
        """,
        (
            target_id,
            1 if next_state["is_favorite"] else 0,
            next_state["progress_episode_number"],
            1 if next_state["watched"] else 0,
            next_state["updated_at"],
        ),
    )
    duplicate_state_ids = [item for item in member_ids if item != target_id]
    if duplicate_state_ids:
        con.execute(
            f"delete from user_title_state where anime_id in ({sql_placeholders(duplicate_state_ids)})",
            duplicate_state_ids,
        )
    con.commit()
    con.close()
    invalidate_catalog_cache(db_path)
    return next_state


class AnimeHandler(BaseHTTPRequestHandler):
    server_version = "AnimeLocal/0.1"

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw or "{}")

    def send_static(self, path):
        safe_path = path.lstrip("/") or "index.html"
        if safe_path == "":
            safe_path = "index.html"
        target = (STATIC_DIR / safe_path).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            self.send_json({"error": "not found"}, 404)
            return
        content = target.read_bytes()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix == ".js":
            ctype = "text/javascript"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/favicon.ico":
            self.send_static("favicon.svg")
            return

        if path == "/api/health":
            self.send_json({"ok": True})
            return

        if path == "/api/anime":
            query = parse_qs(parsed.query).get("q", [""])[0].strip()
            self.send_json({"items": get_anime_list(self.server.db_path, query or None)})
            return

        if path == "/api/recommendations":
            raw_limit = parse_qs(parsed.query).get("limit", [str(DEFAULT_RECOMMENDATION_LIMIT)])[0]
            self.send_json(get_recommendations(self.server.db_path, raw_limit))
            return

        if path.startswith("/api/anime/"):
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "anime":
                detail = get_anime_detail(unquote(parts[2]), self.server.db_path)
                if detail:
                    self.send_json(detail)
                else:
                    self.send_json({"error": "not found"}, 404)
                return

        if path == "/" or re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9-]*", path):
            self.send_static("index.html")
            return
        if path.startswith("/static/"):
            self.send_static(path.removeprefix("/static/"))
            return

        self.send_json({"error": "not found"}, 404)

    def do_PATCH(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/anime/"):
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "anime" and parts[3] == "state":
                try:
                    payload = self.read_json_body()
                    updated = update_user_state(unquote(parts[2]), payload, self.server.db_path)
                except json.JSONDecodeError:
                    self.send_json({"error": "invalid json"}, 400)
                    return
                except ValueError as exc:
                    self.send_json({"error": str(exc)}, 400)
                    return
                if updated is None:
                    self.send_json({"error": "not found"}, 404)
                else:
                    self.send_json({"state": updated})
                return

        self.send_json({"error": "not found"}, 404)


def run(port, host, db_path):
    if not Path(db_path).exists():
        raise SystemExit(f"Database not found: {db_path}")
    server = ThreadingHTTPServer((host, port), AnimeHandler)
    server.db_path = db_path
    print(f"Serving http://{host}:{port} using {db_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description="Run the local AnimeGO SQLite browser.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    args = parser.parse_args()
    run(args.port, args.host, args.db)


if __name__ == "__main__":
    main()
