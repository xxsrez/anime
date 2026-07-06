#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import math
import mimetypes
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "animego.sqlite"
STATIC_DIR = ROOT / "static"
DEFAULT_RECOMMENDATION_LIMIT = 20
MAX_RECOMMENDATION_LIMIT = 50


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def ensure_user_state_schema(con):
    con.execute(
        """
        create table if not exists user_title_state (
            anime_id integer primary key,
            is_favorite integer not null default 0,
            progress_episode_number integer,
            watched integer not null default 0,
            updated_at text not null,
            foreign key (anime_id) references anime(id) on delete cascade
        )
        """
    )
    con.commit()


def ensure_columns(con, table, columns):
    existing = {row[1] for row in con.execute(f"pragma table_info({table})")}
    for column, definition in columns.items():
        if column not in existing:
            con.execute(f"alter table {table} add column {column} {definition}")


def ensure_catalog_schema(con):
    ensure_columns(
        con,
        "anime",
        {
            "source": "text",
            "source_id": "text",
        },
    )
    con.execute("update anime set source = 'animego' where source is null")
    con.execute("update anime set source_id = cast(id as text) where source_id is null")
    con.commit()


def connect(db_path=None):
    path = Path(db_path or os.environ.get("ANIMEGO_DB") or DEFAULT_DB)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    ensure_catalog_schema(con)
    ensure_user_state_schema(con)
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
    return {normalize_key(genre) for genre in item.get("genres", []) if normalize_key(genre)}


def build_recommendation_profile(items):
    seeds = [item for item in items if seed_weight(item) > 0]
    genre_weights = {}
    genre_labels = {}
    kind_weights = {}

    for item in seeds:
        weight = seed_weight(item)
        for genre in item.get("genres", []):
            key = normalize_key(genre)
            if not key:
                continue
            genre_weights[key] = genre_weights.get(key, 0.0) + weight
            genre_labels.setdefault(key, genre)
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
        "kind_weights": kind_weights,
        "top_genres": top_genres,
    }


def genre_profile_score(candidate, profile):
    candidate_keys = item_genre_keys(candidate)
    if not candidate_keys or not profile["genre_weights"]:
        return 0.0, []

    matched_weight = sum(profile["genre_weights"].get(key, 0.0) for key in candidate_keys)
    comparison_size = max(3, len(candidate_keys))
    top_possible = sum(
        sorted(profile["genre_weights"].values(), reverse=True)[:comparison_size]
    )
    score = clamp(matched_weight / top_possible) if top_possible else 0.0
    matched = [
        genre
        for genre in candidate.get("genres", [])
        if normalize_key(genre) in profile["genre_weights"]
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
        matches.append(
            {
                "id": seed["id"],
                "title": seed["title"],
                "score": round(clamp(score), 3),
                "matched_genres": [
                    genre
                    for genre in candidate.get("genres", [])
                    if normalize_key(genre) in overlap
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

    if not reasons:
        reasons.append("Хороший общий рейтинг для стартовой рекомендации")
    return reasons[:4]


def get_recommendations(db_path=None, limit=DEFAULT_RECOMMENDATION_LIMIT):
    limit = normalize_recommendation_limit(limit or DEFAULT_RECOMMENDATION_LIMIT)
    items = get_anime_list(db_path)
    profile = build_recommendation_profile(items)
    has_profile = profile["seed_count"] > 0
    recommendations = []

    for item in items:
        if item.get("is_favorite") or item.get("watched") or item.get("progress_episode_number") is not None:
            continue

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

        score = round(clamp(raw_score) * 100, 1)
        item = dict(item)
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
            "top_genres": profile["top_genres"],
            "mode": "personalized" if has_profile else "popular",
        },
    }


def get_anime_list(db_path=None, q=None):
    con = connect(db_path)
    params = []
    where = ""
    if q:
        where = "where a.title like ? or coalesce(a.subtitle, '') like ?"
        needle = f"%{q}%"
        params.extend([needle, needle])

    rows = con.execute(
        f"""
        select
            a.id,
            a.title,
            a.subtitle,
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
            count(distinct e.id) as episode_count,
            count(distinct case when e.has_video = 1 then e.id end) as available_episode_count,
            count(distinct vs.id) as source_count,
            group_concat(distinct g.genre) as genres
        from anime a
        left join user_title_state us on us.anime_id = a.id
        left join episodes e on e.anime_id = a.id
        left join video_sources vs on vs.anime_id = a.id
        left join anime_genres g on g.anime_id = a.id
        {where}
        group by a.id
        order by source_count > 0 desc, a.id desc
        """,
        params,
    ).fetchall()
    con.close()

    items = rows_to_dicts(rows)
    for item in items:
        apply_state_fields(item)
        item["genres"] = [g for g in (item.pop("genres") or "").split(",") if g]
        item["available_episode_count"] = item["available_episode_count"] or 0
    return items


def get_anime_detail(anime_id, db_path=None):
    con = connect(db_path)
    anime = con.execute("select * from anime where id = ?", (anime_id,)).fetchone()
    if not anime:
        con.close()
        return None

    genres = rows_to_dicts(
        con.execute("select genre from anime_genres where anime_id = ? order by genre", (anime_id,)).fetchall()
    )
    dubbings = rows_to_dicts(
        con.execute("select dubbing from anime_dubbings where anime_id = ? order by dubbing", (anime_id,)).fetchall()
    )
    episodes = rows_to_dicts(
        con.execute(
            """
            select
                e.*,
                count(vs.id) as source_count
            from episodes e
            left join video_sources vs on vs.episode_id = e.id
            where e.anime_id = ?
            group by e.id
            order by cast(e.number as integer), e.id
            """,
            (anime_id,),
        ).fetchall()
    )
    sources = rows_to_dicts(
        con.execute(
            """
            select
                id,
                episode_id,
                provider_id,
                provider_title,
                translation_id,
                translation_title,
                embed_host,
                embed_url,
                embed_url_redacted
            from video_sources
            where anime_id = ?
            order by
                episode_id,
                translation_title,
                case when lower(provider_title) = 'kodik' then 0 else 1 end,
                provider_title
            """,
            (anime_id,),
        ).fetchall()
    )
    fields = rows_to_dicts(
        con.execute(
            "select label, value from anime_fields where anime_id = ? order by label",
            (anime_id,),
        ).fetchall()
    )
    state = con.execute("select * from user_title_state where anime_id = ?", (anime_id,)).fetchone()
    con.close()

    by_episode = {}
    for source in sources:
        by_episode.setdefault(source["episode_id"], []).append(source)

    detail = dict(anime)
    detail.update(normalize_state(state))
    detail["genres"] = [row["genre"] for row in genres]
    detail["dubbings"] = [row["dubbing"] for row in dubbings]
    detail["fields"] = fields
    detail["episodes"] = episodes
    detail["sources_by_episode"] = by_episode
    return detail


def update_user_state(anime_id, patch, db_path=None):
    con = connect(db_path)
    exists = con.execute("select 1 from anime where id = ?", (anime_id,)).fetchone()
    if not exists:
        con.close()
        return None

    current = normalize_state(con.execute("select * from user_title_state where anime_id = ?", (anime_id,)).fetchone())
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
            anime_id,
            1 if next_state["is_favorite"] else 0,
            next_state["progress_episode_number"],
            1 if next_state["watched"] else 0,
            next_state["updated_at"],
        ),
    )
    con.commit()
    con.close()
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
            self.send_response(204)
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
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
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "anime" and parts[2].isdigit():
                detail = get_anime_detail(int(parts[2]), self.server.db_path)
                if detail:
                    self.send_json(detail)
                else:
                    self.send_json({"error": "not found"}, 404)
                return

        if path == "/":
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
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "anime" and parts[2].isdigit() and parts[3] == "state":
                try:
                    payload = self.read_json_body()
                    updated = update_user_state(int(parts[2]), payload, self.server.db_path)
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
