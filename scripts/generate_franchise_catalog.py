#!/usr/bin/env python3
"""Generate curated franchise drafts from a pinned Shikimori manifest.

The manifest owns the editorial choice of franchises plus the Russian summary
and viewing guide.  Shikimori supplies factual release metadata and its
franchise graph.  Generated definitions are checked in so production never
depends on a live third-party request.
"""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
import datetime as dt
import json
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import franchise_catalog  # noqa: E402
import server  # noqa: E402


GRAPHQL_URL = "https://shikimori.io/api/graphql"
REST_ROOT = "https://shikimori.io/api/animes"
DEFAULT_MANIFEST = ROOT / "content" / "franchise-seeds.json"
DEFAULT_OUTPUT_DIR = ROOT / "content" / "franchises"
DEFAULT_DB = ROOT / "db" / "animego.sqlite"
PAGE_SIZE = 50
EXCLUDED_KINDS = {"cm", "music", "pv"}
MAIN_RELATIONS = {"prequel", "sequel"}
KIND_LABELS = {
    "tv": "Сериал",
    "movie": "Фильм",
    "ova": "OVA",
    "ona": "ONA",
    "special": "Спецвыпуск",
    "tv_special": "ТВ-спецвыпуск",
    "web": "Веб-сериал",
}
STATUS_LABELS = {
    "anons": "announced",
    "ongoing": "releasing",
    "released": "finished",
}
MONTHS = (
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
RECAP_RE = re.compile(r"\b(recap|recaps|summary|soushuuhen|дайджест|рекап)\b", re.IGNORECASE)


class GenerationError(RuntimeError):
    pass


class ShikimoriClient:
    def __init__(self, user_agent, request_interval=0.7):
        self.user_agent = user_agent
        self.request_interval = max(0.0, float(request_interval))
        self.last_request_at = 0.0

    def _request_json(self, url, *, data=None):
        wait = self.request_interval - (time.monotonic() - self.last_request_at)
        if wait > 0:
            time.sleep(wait)
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers)
        for attempt in range(4):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    self.last_request_at = time.monotonic()
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                self.last_request_at = time.monotonic()
                if exc.code not in {429, 500, 502, 503, 504} or attempt == 3:
                    raise GenerationError(f"Shikimori request failed: {exc}") from exc
                time.sleep(2 ** attempt)
            except (TimeoutError, urllib.error.URLError) as exc:
                self.last_request_at = time.monotonic()
                if attempt == 3:
                    raise GenerationError(f"Shikimori request failed: {exc}") from exc
                time.sleep(2 ** attempt)
        raise AssertionError("unreachable")

    def graphql(self, query):
        payload = self._request_json(
            GRAPHQL_URL,
            data=json.dumps({"query": query}).encode("utf-8"),
        )
        if payload.get("errors"):
            message = "; ".join(str(item.get("message") or item) for item in payload["errors"])
            raise GenerationError(f"Shikimori GraphQL error: {message}")
        return payload.get("data") or {}

    def franchise_entries(self, franchise):
        entries = {}
        page = 1
        while True:
            query = f"""
                query {{
                  animes(
                    franchise: {json.dumps(franchise)},
                    limit: {PAGE_SIZE},
                    page: {page},
                    order: aired_on
                  ) {{
                    id
                    name
                    russian
                    franchise
                    kind
                    status
                    episodes
                    episodesAired
                    airedOn {{ date }}
                    releasedOn {{ date }}
                    nextEpisodeAt
                    url
                    poster {{ originalUrl }}
                  }}
                }}
            """
            rows = self.graphql(query).get("animes") or []
            for row in rows:
                entries[int(row["id"])] = row
            if len(rows) < PAGE_SIZE:
                break
            page += 1
            if page > 20:
                raise GenerationError(f"Franchise {franchise} exceeded 1000 entries")
        return list(entries.values())

    def franchise_graph(self, anime_id):
        return self._request_json(f"{REST_ROOT}/{int(anime_id)}/franchise")


def load_manifest(path):
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GenerationError(f"Unable to read manifest {path}: {exc}") from exc
    if manifest.get("schema_version") != 1:
        raise GenerationError("Manifest must use schema_version 1")
    items = manifest.get("items")
    if not isinstance(items, list) or len(items) != 40:
        raise GenerationError("Manifest must contain exactly 40 franchise items")
    ranks = [item.get("rank") for item in items]
    if sorted(ranks) != list(range(1, 41)):
        raise GenerationError("Manifest ranks must be exactly 1..40")
    slugs = [item.get("slug") for item in items]
    if len(slugs) != len(set(slugs)):
        raise GenerationError("Manifest contains duplicate slugs")
    return manifest


def normalize_text(value):
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def iso_date(value):
    text = normalize_text((value or {}).get("date") if isinstance(value, dict) else value)
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise GenerationError(f"Invalid Shikimori date {text!r}") from exc


def format_date(value, *, include_year=True):
    parsed = dt.date.fromisoformat(value)
    suffix = f" {parsed.year}" if include_year else ""
    return f"{parsed.day} {MONTHS[parsed.month]}{suffix}"


def release_label(start, end, year):
    if start and end and start != end:
        start_date = dt.date.fromisoformat(start)
        end_date = dt.date.fromisoformat(end)
        if start_date.year == end_date.year:
            return f"{format_date(start, include_year=False)} — {format_date(end)}"
        return f"{format_date(start)} — {format_date(end)}"
    if start:
        return format_date(start)
    if year:
        return str(year)
    return "Дата не объявлена"


def entry_sort_key(item):
    start = iso_date(item.get("airedOn"))
    end = iso_date(item.get("releasedOn"))
    return (start or end or "9999-12-31", int(item["id"]))


def graph_roles(graph, primary_ids):
    main_graph = defaultdict(set)
    recap_ids = set()
    spinoff_ids = set()
    optional_ids = set()
    for link in graph.get("links") or []:
        source_id = int(link["source_id"])
        target_id = int(link["target_id"])
        relation = normalize_text(link.get("relation")).casefold()
        if relation in MAIN_RELATIONS:
            main_graph[source_id].add(target_id)
            main_graph[target_id].add(source_id)
        elif relation == "summary":
            recap_ids.add(target_id)
        elif relation == "full_story":
            recap_ids.add(source_id)
        elif relation == "spin_off":
            spinoff_ids.add(target_id)
        elif relation == "parent_story":
            spinoff_ids.add(source_id)
        elif relation in {"alternative_setting", "alternative_version"}:
            spinoff_ids.add(target_id)
        elif relation in {"character", "other", "side_story"}:
            optional_ids.add(target_id)

    main_ids = set()
    queue = deque(int(value) for value in primary_ids)
    while queue:
        anime_id = queue.popleft()
        if anime_id in main_ids:
            continue
        main_ids.add(anime_id)
        queue.extend(main_graph.get(anime_id) or ())
    return {
        "main": main_ids,
        "recap": recap_ids,
        "spinoff": spinoff_ids,
        "optional": optional_ids,
    }


def story_role(item, roles):
    anime_id = int(item["id"])
    name = f"{normalize_text(item.get('name'))} {normalize_text(item.get('russian'))}"
    if anime_id in roles["recap"] or RECAP_RE.search(name):
        return "recap"
    if anime_id in roles["main"]:
        return "main"
    if anime_id in roles["spinoff"]:
        return "spinoff"
    return "optional"


def kind_label(value):
    value = normalize_text(value).casefold()
    return KIND_LABELS.get(value, value.upper() if value else "Аниме")


def entry_summary(role, kind, *, first_main=False):
    if role == "recap":
        return "Краткое переизложение уже вышедших событий франшизы."
    if role == "spinoff":
        return "Отдельная ветка франшизы с другим фокусом, героями или версией истории."
    if role == "main":
        if first_main:
            return "Старт одной из основных сюжетных линий франшизы."
        return "Продолжение основной сюжетной линии франшизы."
    if kind == "Фильм":
        return "Полнометражная история внутри франшизы; её место в каноне зависит от выбранной ветки."
    if kind in {"OVA", "ONA", "Спецвыпуск", "ТВ-спецвыпуск"}:
        return "Дополнительный выпуск, расширяющий мир или истории героев франшизы."
    return "Дополнительная работа внутри франшизы, не обязательная для основной линии."


def watch_note(role, *, first_main=False):
    if role == "recap":
        return "Необязательно: можно пропустить, если хорошо помните предыдущие события."
    if role == "spinoff":
        return "Самостоятельная ветка; её место в маршруте указано в инструкции вверху карточки."
    if role == "main":
        if first_main:
            return "Ранняя точка входа в одну из основных линий; сверьтесь с инструкцией вверху карточки."
        return "Часть одной из основных линий; место в маршруте указано в инструкции вверху карточки."
    return "Необязательно для основной линии; безопаснее смотреть по порядку выхода."


def status_note(status, start, next_episode_at):
    if status == "announced":
        if start:
            return f"Анонсировано; заявленная дата выхода — {format_date(start)}."
        return "Анонсировано; точная дата выхода пока не указана."
    if status == "releasing":
        next_date = normalize_text(next_episode_at)[:10]
        try:
            if next_date:
                return f"Выходит; следующая серия ожидается {format_date(next_date)}."
        except ValueError:
            pass
        return "Сейчас выходит; расписание следующих серий может уточняться."
    return None


def catalog_match_index(db_path):
    if not db_path or not Path(db_path).is_file():
        return {}, {}
    items = server.get_anime_list(db_path)
    groups_by_member = {}
    group_context = {}
    for item in items:
        member_ids = {
            int(value)
            for value in item.get("source_member_ids") or [item.get("id")]
            if value is not None
        }
        source_keys = {
            f"{variant.get('source')}:{variant.get('source_id')}"
            for variant in [item, *(item.get("source_variants") or [])]
            if variant.get("source") and variant.get("source_id") is not None
        }
        names = {
            server.normalize_match_title(value)
            for variant in [item, *(item.get("source_variants") or [])]
            for value in (variant.get("title"), variant.get("subtitle"))
            if server.normalize_match_title(value)
        }
        years = {
            int(match.group(0))
            for variant in [item, *(item.get("source_variants") or [])]
            for match in [re.search(r"\b(?:19|20)\d{2}\b", str(variant.get("year") or ""))]
            if match
        }
        group_id = int(item["id"])
        group_context[group_id] = {
            "id": group_id,
            "member_ids": member_ids,
            "source_keys": source_keys,
            "names": names,
            "years": years,
        }
        for member_id in member_ids:
            groups_by_member[member_id] = group_context[group_id]

    con = server.connect(db_path)
    try:
        rows = con.execute("select anime_id, alias from anime_title_aliases").fetchall()
    finally:
        con.close()
    for row in rows:
        context = groups_by_member.get(int(row["anime_id"]))
        normalized = server.normalize_match_title(row["alias"])
        if context and normalized:
            context["names"].add(normalized)

    by_name = defaultdict(dict)
    for context in group_context.values():
        for name in context["names"]:
            by_name[name][context["id"]] = context
    return by_name, group_context


def exact_source_match(item, by_name, *, claimed_group_ids):
    names = {
        server.normalize_match_title(value)
        for value in (item.get("russian"), item.get("name"))
        if server.normalize_match_title(value)
    }
    candidates = {}
    for name in names:
        candidates.update(by_name.get(name) or {})
    if len(candidates) > 1:
        year_text = (iso_date(item.get("airedOn")) or "")[:4]
        year = int(year_text) if year_text else None
        same_year = {
            group_id: context
            for group_id, context in candidates.items()
            if year is not None and year in context["years"]
        }
        if len(same_year) == 1:
            candidates = same_year
    if len(candidates) != 1:
        return []
    group_id, context = next(iter(candidates.items()))
    if group_id in claimed_group_ids:
        return []
    claimed_group_ids.add(group_id)
    return sorted(context["source_keys"])


def build_definition(seed, manifest, client, by_name):
    source_entries = client.franchise_entries(seed["shikimori_franchise"])
    entries = [
        item
        for item in source_entries
        if normalize_text(item.get("kind")).casefold() not in EXCLUDED_KINDS
        and int(item["id"]) not in {int(value) for value in seed.get("exclude_shikimori_ids") or []}
    ]
    if len(entries) < 2:
        raise GenerationError(f"{seed['slug']} has fewer than two usable releases")
    entry_ids = {int(item["id"]) for item in entries}
    primary_ids = [
        int(value)
        for value in seed.get("primary_shikimori_ids") or [seed["primary_shikimori_id"]]
    ]
    if not any(value in entry_ids for value in primary_ids):
        raise GenerationError(f"{seed['slug']} primary ID is outside its franchise tag")
    graph = client.franchise_graph(seed["primary_shikimori_id"])
    roles = graph_roles(graph, primary_ids)
    entries.sort(key=entry_sort_key)

    role_by_id = {int(item["id"]): story_role(item, roles) for item in entries}
    first_main_id = next(
        (int(item["id"]) for item in entries if role_by_id[int(item["id"])] == "main"),
        primary_ids[0],
    )
    claimed_group_ids = set()
    output_entries = []
    for release_order, item in enumerate(entries, 1):
        anime_id = int(item["id"])
        start = iso_date(item.get("airedOn"))
        end = iso_date(item.get("releasedOn"))
        year = int((start or end)[:4]) if start or end else None
        role = role_by_id[anime_id]
        kind = kind_label(item.get("kind"))
        status = STATUS_LABELS.get(normalize_text(item.get("status")).casefold(), "unknown")
        title = normalize_text(item.get("russian")) or normalize_text(item.get("name"))
        subtitle = normalize_text(item.get("name"))
        first_main = anime_id == first_main_id
        match = {
            "shikimori": [anime_id],
            "sources": exact_source_match(item, by_name, claimed_group_ids=claimed_group_ids),
        }
        output = {
            "key": f"shikimori-{anime_id}",
            "title": title,
            "subtitle": subtitle if subtitle and subtitle.casefold() != title.casefold() else None,
            "summary": entry_summary(role, kind, first_main=first_main),
            "kind": kind,
            "status": status,
            "release_date": start,
            "release_end_date": end if start and end and end != start else None,
            "release_year": year,
            "release_label": release_label(start, end, year),
            "episode_count": int(item.get("episodes") or 0) or None,
            "release_order": release_order,
            # A franchise tag can contain several unrelated continuities.  The
            # safe generated fallback is release order; the curated guide owns
            # branch-specific viewing advice.  Hand-authored definitions may
            # still provide a distinct recommended order.
            "watch_order": release_order,
            "story_role": role,
            "watch_note": watch_note(role, first_main=first_main),
            "status_note": status_note(status, start, item.get("nextEpisodeAt")),
            "cover_url": (item.get("poster") or {}).get("originalUrl"),
            "match": match,
        }
        output_entries.append({key: value for key, value in output.items() if value not in (None, [], {})})

    primary = next(item for item in entries if int(item["id"]) == primary_ids[0])
    updated_at = manifest["updated_at"]
    definition = {
        "schema_version": 1,
        "slug": seed["slug"],
        "title": seed["title"],
        "short_title": seed.get("short_title"),
        "original_title": seed.get("original_title"),
        "summary": seed["summary"],
        "guide": seed["guide"],
        "cover_url": (primary.get("poster") or {}).get("originalUrl"),
        "updated_at": updated_at,
        "source": {
            "title": "Shikimori: карточки и граф связей релизов",
            "url": f"https://shikimori.io/animes/{primary_ids[0]}",
            "updated_at": updated_at,
        },
        "data_origin": {
            "provider": "shikimori",
            "franchise": seed["shikimori_franchise"],
            "primary_shikimori_ids": primary_ids,
            "fetched_at": updated_at,
        },
        "selection_rank": seed["rank"],
        "entries": output_entries,
        "announcements": [],
    }
    definition = {key: value for key, value in definition.items() if value not in (None, "")}
    return franchise_catalog.validate_definition(definition, f"generated {seed['slug']}")


def generated_json(definition):
    return json.dumps(definition, ensure_ascii=False, indent=2) + "\n"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--user-agent", default="AnimeLocal/1.0 (contact: xxsrez@gmail.com)")
    parser.add_argument("--request-interval", type=float, default=0.7)
    parser.add_argument(
        "--slug",
        action="append",
        help="generate only this manifest slug; repeat to select multiple",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="write generated definitions")
    mode.add_argument("--check", action="store_true", help="fail if checked-in definitions differ")
    return parser.parse_args()


def main():
    args = parse_args()
    manifest = load_manifest(args.manifest)
    selected_slugs = set(args.slug or [])
    known_slugs = {item["slug"] for item in manifest["items"]}
    unknown_slugs = selected_slugs - known_slugs
    if unknown_slugs:
        raise GenerationError("Unknown manifest slug(s): " + ", ".join(sorted(unknown_slugs)))
    by_name, _ = catalog_match_index(args.db)
    client = ShikimoriClient(args.user_agent, args.request_interval)
    definitions = []
    stats = []
    seeds = [
        seed
        for seed in sorted(manifest["items"], key=lambda item: item["rank"])
        if not selected_slugs or seed["slug"] in selected_slugs
    ]
    for seed in seeds:
        definition = build_definition(seed, manifest, client, by_name)
        definitions.append(definition)
        stats.append(
            {
                "rank": seed["rank"],
                "slug": seed["slug"],
                "entries": len(definition["entries"]),
                "source_matches": sum(bool(entry.get("match", {}).get("sources")) for entry in definition["entries"]),
                "active": sum(entry.get("status") in {"releasing", "announced"} for entry in definition["entries"]),
            }
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    drift = []
    for definition in definitions:
        path = args.output_dir / f"{definition['slug']}.json"
        expected = generated_json(definition)
        if args.write:
            path.write_text(expected, encoding="utf-8")
        elif args.check:
            actual = path.read_text(encoding="utf-8") if path.is_file() else None
            if actual != expected:
                drift.append(str(path.relative_to(ROOT)))

    print(json.dumps({"franchises": len(definitions), "stats": stats}, ensure_ascii=False, indent=2))
    if drift:
        raise SystemExit("Generated franchise definitions differ: " + ", ".join(drift))


if __name__ == "__main__":
    main()
