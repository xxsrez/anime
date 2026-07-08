#!/usr/bin/env python3
import argparse
import json
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server


DEFAULT_DB = ROOT / "db" / "animego.sqlite"
DEFAULT_CACHE_DIR = ROOT / "data" / "external" / "title_aliases"
USER_AGENT = "AnimeLocalAliasEnricher/0.1"
AOD_JSONL_URL = "https://github.com/manami-project/anime-offline-database/releases/latest/download/anime-offline-database.jsonl"
ANIDB_TITLES_URL = "https://raw.githubusercontent.com/c032/anidb-animetitles-archive/main/data/animetitles.json"
SHIKIMORI_ANIMES_URL = "https://shikimori.one/api/animes"
AOD_CACHE_NAME = "anime-offline-database.jsonl"
ANIDB_CACHE_NAME = "anidb-animetitles.jsonl"
ALLOWED_ANIDB_LANGUAGES = {"ru", "en", "ja", "x-jat"}
SHIKIMORI_BATCH_SIZE = 50


def http_get(url, timeout=60, retries=4):
    for attempt in range(retries):
        request = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read()
        except HTTPError as exc:
            if exc.code != 429 or attempt == retries - 1:
                raise
            retry_after = exc.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** (attempt + 1)
            time.sleep(delay)
    raise RuntimeError(f"failed to fetch {url}")


def cached_download(url, path, refresh=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not refresh:
        return path
    data = http_get(url)
    path.write_bytes(data)
    return path


def normalize(value):
    return server.normalize_search_text(value)


def parse_year(value):
    match = re.search(r"\d{4}", str(value or ""))
    return int(match.group(0)) if match else None


def parse_int(value):
    match = re.search(r"\d+", str(value or ""))
    return int(match.group(0)) if match else None


def parse_source_id(sources, pattern):
    for source in sources or []:
        match = re.search(pattern, source)
        if match:
            return int(match.group(1))
    return None


def load_anime_rows(con):
    rows = con.execute(
        """
        select id, title, subtitle, year, kind, episodes_text, source
        from anime
        order by id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def build_match_key_index(anime_rows):
    index = defaultdict(list)
    for row in anime_rows:
        for field, base_score in (("title", 14), ("subtitle", 12)):
            key = normalize(row.get(field))
            if key and len(key) >= 3:
                index[key].append((row["id"], field, base_score))
    return index


def aod_entries(path):
    with path.open("r", encoding="utf-8") as handle:
        metadata = json.loads(handle.readline())
        for line in handle:
            if line.strip():
                yield metadata, json.loads(line)


def aod_entry_ids(entry):
    sources = entry.get("sources") or []
    return {
        "mal_id": parse_source_id(sources, r"myanimelist\.net/anime/(\d+)"),
        "anidb_id": parse_source_id(sources, r"anidb\.net/anime/(\d+)"),
        "anilist_id": parse_source_id(sources, r"anilist\.co/anime/(\d+)"),
    }


def aod_match_keys(entry):
    keys = []
    main_key = normalize(entry.get("title"))
    if main_key:
        keys.append((main_key, "main"))
    for synonym in entry.get("synonyms") or []:
        key = normalize(synonym)
        if key and len(key) >= 3:
            keys.append((key, "synonym"))
    return set(keys)


def match_aod_entries(anime_rows, entries):
    rows_by_id = {row["id"]: row for row in anime_rows}
    key_index = build_match_key_index(anime_rows)
    candidates = defaultdict(list)
    entry_count = 0

    for entry in entries:
        entry_count += 1
        ids = aod_entry_ids(entry)
        entry_year = (entry.get("animeSeason") or {}).get("year")
        entry_episodes = entry.get("episodes")
        for key, key_kind in aod_match_keys(entry):
            for anime_id, field, base_score in key_index.get(key, []):
                row = rows_by_id[anime_id]
                row_year = parse_year(row.get("year"))
                if row_year and entry_year and abs(row_year - entry_year) > 1:
                    continue
                score = base_score
                if key_kind == "main":
                    score += 5
                if field == "subtitle":
                    score += 2
                if row_year and entry_year and row_year == entry_year:
                    score += 4
                row_episodes = parse_int(row.get("episodes_text"))
                if row_episodes and entry_episodes and row_episodes == entry_episodes:
                    score += 1
                candidates[anime_id].append(
                    {
                        **ids,
                        "score": score,
                        "title": entry.get("title"),
                        "year": entry_year,
                        "episodes": entry_episodes,
                    }
                )

    matches = {}
    ambiguous = {}
    for anime_id, anime_candidates in candidates.items():
        ranked = sorted(anime_candidates, key=lambda candidate: candidate["score"], reverse=True)
        top = ranked[0]
        ties = [
            candidate
            for candidate in ranked[1:]
            if candidate["score"] == top["score"]
            and (
                candidate.get("mal_id"),
                candidate.get("anidb_id"),
                candidate.get("anilist_id"),
            )
            != (top.get("mal_id"), top.get("anidb_id"), top.get("anilist_id"))
        ]
        if ties:
            ambiguous[anime_id] = [top] + ties
            continue
        matches[anime_id] = top

    return {
        "entry_count": entry_count,
        "candidate_count": len(candidates),
        "matches": matches,
        "ambiguous": ambiguous,
    }


def load_anidb_titles(path):
    entries = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            entry = json.loads(line)
            entries[entry["id"]] = entry
    return entries


def alias_type_for_anidb_title(title):
    language = title.get("language")
    title_type = title.get("type") or "syn"
    if language == "ru":
        return "ru_alt" if title_type in {"main", "official"} else "synonym"
    if language == "en":
        return "english" if title_type in {"main", "official"} else "synonym"
    if language == "ja":
        return "native" if title_type in {"main", "official", "kana"} else "synonym"
    if language == "x-jat":
        return "romaji" if title_type == "main" else "synonym"
    return "synonym"


def language_for_anidb_title(title):
    language = title.get("language")
    return "ja-Latn" if language == "x-jat" else language


def make_alias(anime_id, alias, language, alias_type, source, source_ref, confidence=0.9):
    normalized = normalize(alias)
    if not normalized:
        return None
    return {
        "anime_id": anime_id,
        "alias": str(alias).strip(),
        "normalized_alias": normalized,
        "language": language,
        "alias_type": alias_type,
        "source": source,
        "source_ref": source_ref,
        "confidence": confidence,
    }


def duplicate_of_existing_title(row, alias):
    alias_key = normalize(alias)
    return alias_key and alias_key in {normalize(row.get("title")), normalize(row.get("subtitle"))}


def build_anidb_aliases(anime_rows_by_id, aod_matches, anidb_entries):
    aliases = []
    missing_anidb = 0
    for anime_id, match in aod_matches.items():
        anidb_id = match.get("anidb_id")
        if not anidb_id:
            missing_anidb += 1
            continue
        entry = anidb_entries.get(anidb_id)
        if not entry:
            missing_anidb += 1
            continue
        row = anime_rows_by_id[anime_id]
        for title in entry.get("titles") or []:
            language = title.get("language")
            alias = title.get("title")
            if language not in ALLOWED_ANIDB_LANGUAGES:
                continue
            if duplicate_of_existing_title(row, alias):
                continue
            aliases.append(
                make_alias(
                    anime_id,
                    alias,
                    language_for_anidb_title(title),
                    alias_type_for_anidb_title(title),
                    "anidb",
                    f"anidb:{anidb_id}:{language}:{title.get('type') or 'syn'}",
                    confidence=0.88,
                )
            )
    return [alias for alias in aliases if alias], missing_anidb


def load_existing_remote_ids(con):
    rows = con.execute(
        """
        select anime_id, label, value
        from anime_fields
        where label in ('MyAnimeList ID', 'Shikimori ID')
          and value is not null
          and trim(value) <> ''
        """
    ).fetchall()
    ids_by_anime_id = defaultdict(set)
    for row in rows:
        value = parse_int(row["value"])
        if value:
            ids_by_anime_id[row["anime_id"]].add(value)
    return ids_by_anime_id


def shikimori_batches(mal_ids, delay=1.1):
    ids = sorted({int(value) for value in mal_ids if value})
    for offset in range(0, len(ids), SHIKIMORI_BATCH_SIZE):
        batch = ids[offset : offset + SHIKIMORI_BATCH_SIZE]
        query = urlencode({"ids": ",".join(str(value) for value in batch), "limit": SHIKIMORI_BATCH_SIZE})
        data = http_get(f"{SHIKIMORI_ANIMES_URL}?{query}", timeout=30)
        yield json.loads(data.decode("utf-8"))
        if delay:
            time.sleep(delay)


def build_shikimori_list_aliases(anime_rows_by_id, mal_ids_by_anime_id, delay=1.1):
    anime_ids_by_mal_id = defaultdict(set)
    for anime_id, mal_ids in mal_ids_by_anime_id.items():
        for mal_id in mal_ids:
            anime_ids_by_mal_id[int(mal_id)].add(anime_id)

    aliases = []
    response_count = 0
    for batch in shikimori_batches(anime_ids_by_mal_id, delay=delay):
        response_count += 1
        for entry in batch:
            mal_id = entry.get("id")
            for anime_id in anime_ids_by_mal_id.get(int(mal_id or 0), []):
                row = anime_rows_by_id[anime_id]
                russian = entry.get("russian")
                if russian and not duplicate_of_existing_title(row, russian):
                    aliases.append(
                        make_alias(
                            anime_id,
                            russian,
                            "ru",
                            "ru_alt",
                            "shikimori",
                            f"shikimori:{mal_id}:russian",
                            confidence=0.92,
                        )
                    )
                name = entry.get("name")
                if name and not duplicate_of_existing_title(row, name):
                    aliases.append(
                        make_alias(
                            anime_id,
                            name,
                            "ja-Latn",
                            "romaji",
                            "shikimori",
                            f"shikimori:{mal_id}:name",
                            confidence=0.9,
                        )
                    )
    return [alias for alias in aliases if alias], response_count


def alias_key(alias):
    return (
        alias["anime_id"],
        alias["normalized_alias"],
        alias["source"],
        alias["alias_type"],
    )


def unique_aliases(aliases):
    by_key = {}
    for alias in aliases:
        key = alias_key(alias)
        existing = by_key.get(key)
        if existing is None or alias["confidence"] > existing["confidence"]:
            by_key[key] = alias
    return list(by_key.values())


def upsert_aliases(con, aliases, dry_run=False):
    if dry_run:
        return 0
    timestamp = server.now_iso()
    before = con.total_changes
    for alias in aliases:
        con.execute(
            """
            insert into anime_title_aliases (
                anime_id, alias, normalized_alias, language, alias_type, source,
                source_ref, confidence, created_at, updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(anime_id, normalized_alias, source, alias_type) do update set
                alias = excluded.alias,
                language = excluded.language,
                source_ref = excluded.source_ref,
                confidence = excluded.confidence,
                updated_at = excluded.updated_at
            where anime_title_aliases.alias is not excluded.alias
               or anime_title_aliases.language is not excluded.language
               or anime_title_aliases.source_ref is not excluded.source_ref
               or anime_title_aliases.confidence is not excluded.confidence
            """,
            (
                alias["anime_id"],
                alias["alias"],
                alias["normalized_alias"],
                alias.get("language"),
                alias["alias_type"],
                alias["source"],
                alias.get("source_ref"),
                alias.get("confidence", 0.9),
                timestamp,
                timestamp,
            ),
        )
    return con.total_changes - before


def write_ambiguous_report(path, anime_rows_by_id, ambiguous):
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for anime_id, candidates in sorted(ambiguous.items()):
            row = anime_rows_by_id[anime_id]
            handle.write(
                json.dumps(
                    {
                        "anime_id": anime_id,
                        "title": row.get("title"),
                        "subtitle": row.get("subtitle"),
                        "candidates": candidates,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )


def run(args):
    con = server.connect(args.db)
    try:
        anime_rows = load_anime_rows(con)
        anime_rows_by_id = {row["id"]: row for row in anime_rows}

        aod_path = cached_download(AOD_JSONL_URL, args.cache_dir / AOD_CACHE_NAME, refresh=args.refresh)
        entries = (entry for _, entry in aod_entries(aod_path))
        aod_result = match_aod_entries(anime_rows, entries)
        matches = aod_result["matches"]

        all_aliases = []
        stats = {
            "anime_rows": len(anime_rows),
            "aod_entries": aod_result["entry_count"],
            "aod_candidates": aod_result["candidate_count"],
            "aod_matches": len(matches),
            "aod_ambiguous": len(aod_result["ambiguous"]),
        }

        if "anidb" in args.sources:
            anidb_path = cached_download(ANIDB_TITLES_URL, args.cache_dir / ANIDB_CACHE_NAME, refresh=args.refresh)
            anidb_entries = load_anidb_titles(anidb_path)
            anidb_aliases, missing_anidb = build_anidb_aliases(anime_rows_by_id, matches, anidb_entries)
            all_aliases.extend(anidb_aliases)
            stats["anidb_entries"] = len(anidb_entries)
            stats["anidb_aliases"] = len(anidb_aliases)
            stats["anidb_missing_for_matches"] = missing_anidb

        mal_ids_by_anime_id = defaultdict(set)
        for anime_id, match in matches.items():
            if match.get("mal_id"):
                mal_ids_by_anime_id[anime_id].add(match["mal_id"])
        for anime_id, ids in load_existing_remote_ids(con).items():
            mal_ids_by_anime_id[anime_id].update(ids)

        if "shikimori-list" in args.sources:
            shikimori_aliases, shikimori_batches_count = build_shikimori_list_aliases(
                anime_rows_by_id,
                mal_ids_by_anime_id,
                delay=args.shikimori_delay,
            )
            all_aliases.extend(shikimori_aliases)
            stats["shikimori_mal_ids"] = len({mal_id for ids in mal_ids_by_anime_id.values() for mal_id in ids})
            stats["shikimori_batches"] = shikimori_batches_count
            stats["shikimori_aliases"] = len(shikimori_aliases)

        aliases = unique_aliases(all_aliases)
        stats["unique_aliases"] = len(aliases)
        stats["changed_rows"] = upsert_aliases(con, aliases, dry_run=args.dry_run)
        if args.dry_run:
            con.rollback()
        else:
            con.commit()

        if args.ambiguous_report:
            write_ambiguous_report(args.ambiguous_report, anime_rows_by_id, aod_result["ambiguous"])

        print(json.dumps(stats, ensure_ascii=False, sort_keys=True, indent=2))
    finally:
        con.close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Enrich anime_title_aliases from external title metadata sources.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--refresh", action="store_true", help="re-download cached source datasets")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--source",
        dest="sources",
        action="append",
        choices=["anidb", "shikimori-list"],
        help="source to use; repeatable. Defaults to both safe bulk sources.",
    )
    parser.add_argument("--shikimori-delay", type=float, default=1.1)
    parser.add_argument(
        "--ambiguous-report",
        type=Path,
        default=ROOT / "data" / "external" / "title_aliases" / "ambiguous-aod-matches.jsonl",
    )
    args = parser.parse_args(argv)
    args.sources = set(args.sources or ["anidb", "shikimori-list"])
    return args


if __name__ == "__main__":
    run(parse_args())
