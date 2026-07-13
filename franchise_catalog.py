"""Curated franchise metadata and matching helpers.

Franchises intentionally live above the canonical catalog layer.  A checked-in
definition can describe releases that are not playable (or not imported) yet,
while stable upstream IDs connect the entries that do exist to canonical
catalog items.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import re
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_FRANCHISE_DIR = ROOT / "content" / "franchises"
FRANCHISE_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
EXTERNAL_REF_RE = re.compile(r"^(anidb|shikimori):([^:]+):")
VALID_ENTRY_STATUSES = {"announced", "finished", "releasing", "unknown"}
VALID_STORY_ROLES = {"main", "optional", "recap", "spinoff"}

_CACHE_LOCK = threading.RLock()
_DEFINITION_CACHE = {}


class FranchiseDataError(ValueError):
    pass


def _nonempty_text(value, field):
    text = str(value or "").strip()
    if not text:
        raise FranchiseDataError(f"{field} must be a non-empty string")
    return text


def _positive_order(value, field):
    if type(value) is not int or value < 1:
        raise FranchiseDataError(f"{field} must be a positive integer")
    return value


def _validate_source_match(value, field):
    text = _nonempty_text(value, field)
    if ":" not in text or not all(part.strip() for part in text.split(":", 1)):
        raise FranchiseDataError(f"{field} must use source:source_id")
    return text


def _optional_https_url(value, field):
    if value in (None, ""):
        return None
    text = _nonempty_text(value, field)
    if not text.startswith("https://"):
        raise FranchiseDataError(f"{field} must use https")
    return text


def _optional_iso_date(value, field):
    if value in (None, ""):
        return None
    text = _nonempty_text(value, field)
    try:
        parsed = dt.date.fromisoformat(text)
    except ValueError as exc:
        raise FranchiseDataError(f"{field} must use YYYY-MM-DD") from exc
    if parsed.isoformat() != text:
        raise FranchiseDataError(f"{field} must use YYYY-MM-DD")
    return parsed


def _optional_release_year(value, field):
    if value is None:
        return None
    if type(value) is not int or not 1900 <= value <= 3000:
        raise FranchiseDataError(f"{field} must be a year between 1900 and 3000")
    return value


def validate_definition(raw, source="franchise definition"):
    if not isinstance(raw, dict):
        raise FranchiseDataError(f"{source} must be an object")
    definition = copy.deepcopy(raw)
    if definition.get("schema_version") != 1:
        raise FranchiseDataError(f"{source} has an unsupported schema_version")
    slug = _nonempty_text(definition.get("slug"), f"{source}.slug")
    if not FRANCHISE_SLUG_RE.fullmatch(slug):
        raise FranchiseDataError(f"{source}.slug is invalid")
    definition["slug"] = slug
    definition["title"] = _nonempty_text(definition.get("title"), f"{source}.title")
    definition["summary"] = _nonempty_text(definition.get("summary"), f"{source}.summary")
    definition["official_url"] = _optional_https_url(
        definition.get("official_url"),
        f"{source}.official_url",
    )
    if definition.get("cover_url") is not None:
        definition["cover_url"] = _optional_https_url(
            definition.get("cover_url"),
            f"{source}.cover_url",
        )
    if definition.get("updated_at") is not None:
        definition["updated_at"] = _optional_iso_date(
            definition.get("updated_at"),
            f"{source}.updated_at",
        ).isoformat()
    source_info = definition.get("source") or {}
    if not isinstance(source_info, dict):
        raise FranchiseDataError(f"{source}.source must be an object")
    source_info = copy.deepcopy(source_info)
    if source_info.get("title") is not None:
        source_info["title"] = _nonempty_text(source_info["title"], f"{source}.source.title")
    if source_info.get("url") is not None:
        source_info["url"] = _optional_https_url(source_info["url"], f"{source}.source.url")
    if source_info.get("updated_at") is not None:
        source_info["updated_at"] = _optional_iso_date(
            source_info["updated_at"],
            f"{source}.source.updated_at",
        ).isoformat()
    definition["source"] = source_info
    entries = definition.get("entries")
    if not isinstance(entries, list) or len(entries) < 2:
        raise FranchiseDataError(f"{source}.entries must contain at least two releases")

    entry_keys = set()
    release_orders = set()
    watch_orders = set()
    generated_from_shikimori = (definition.get("data_origin") or {}).get("provider") == "shikimori"
    for index, entry in enumerate(entries):
        field = f"{source}.entries[{index}]"
        if not isinstance(entry, dict):
            raise FranchiseDataError(f"{field} must be an object")
        key = _nonempty_text(entry.get("key"), f"{field}.key")
        if key in entry_keys:
            raise FranchiseDataError(f"{source} has duplicate entry key {key}")
        entry_keys.add(key)
        entry["key"] = key
        entry["title"] = _nonempty_text(entry.get("title"), f"{field}.title")
        release_date = _optional_iso_date(entry.get("release_date"), f"{field}.release_date")
        release_end_date = _optional_iso_date(
            entry.get("release_end_date"),
            f"{field}.release_end_date",
        )
        release_year = _optional_release_year(entry.get("release_year"), f"{field}.release_year")
        if release_end_date and not release_date:
            raise FranchiseDataError(f"{field}.release_end_date requires release_date")
        if release_date and release_end_date and release_end_date < release_date:
            raise FranchiseDataError(f"{field}.release_end_date cannot precede release_date")
        if release_date and release_year is not None and release_date.year != release_year:
            raise FranchiseDataError(f"{field}.release_year must match release_date")
        if release_date:
            entry["release_date"] = release_date.isoformat()
        if release_end_date:
            entry["release_end_date"] = release_end_date.isoformat()
        if entry.get("official_url") is not None:
            entry["official_url"] = _optional_https_url(
                entry.get("official_url"),
                f"{field}.official_url",
            )
        if entry.get("cover_url") is not None:
            entry["cover_url"] = _optional_https_url(
                entry.get("cover_url"),
                f"{field}.cover_url",
            )
        episode_count = entry.get("episode_count")
        if episode_count is not None and (type(episode_count) is not int or episode_count < 1):
            raise FranchiseDataError(f"{field}.episode_count must be a positive integer or null")
        release_order = _positive_order(entry.get("release_order"), f"{field}.release_order")
        if release_order in release_orders:
            raise FranchiseDataError(f"{source} has duplicate release_order {release_order}")
        release_orders.add(release_order)
        if entry.get("watch_order") is not None:
            watch_order = _positive_order(entry["watch_order"], f"{field}.watch_order")
            if watch_order in watch_orders:
                raise FranchiseDataError(f"{source} has duplicate watch_order {watch_order}")
            watch_orders.add(watch_order)
        status = entry.get("status", "unknown")
        if status not in VALID_ENTRY_STATUSES:
            raise FranchiseDataError(f"{field}.status is invalid")
        entry["status"] = status
        role = entry.get("story_role", "optional")
        if role not in VALID_STORY_ROLES:
            raise FranchiseDataError(f"{field}.story_role is invalid")
        entry["story_role"] = role
        match = entry.get("match") or {}
        if not isinstance(match, dict):
            raise FranchiseDataError(f"{field}.match must be an object")
        match["anidb"] = [int(value) for value in match.get("anidb") or []]
        if any(value < 1 for value in match["anidb"]):
            raise FranchiseDataError(f"{field}.match.anidb must contain positive IDs")
        if len(match["anidb"]) != len(set(match["anidb"])):
            raise FranchiseDataError(f"{field}.match.anidb contains duplicate IDs")
        match["shikimori"] = [int(value) for value in match.get("shikimori") or []]
        if any(value < 1 for value in match["shikimori"]):
            raise FranchiseDataError(f"{field}.match.shikimori must contain positive IDs")
        if len(match["shikimori"]) != len(set(match["shikimori"])):
            raise FranchiseDataError(f"{field}.match.shikimori contains duplicate IDs")
        match["sources"] = [
            _validate_source_match(value, f"{field}.match.sources")
            for value in match.get("sources") or []
        ]
        if len(match["sources"]) != len(set(match["sources"])):
            raise FranchiseDataError(f"{field}.match.sources contains duplicate keys")
        if generated_from_shikimori:
            if not match["shikimori"]:
                raise FranchiseDataError(f"{field}.match.shikimori is required for generated entries")
            for required in ("summary", "kind", "watch_note"):
                _nonempty_text(entry.get(required), f"{field}.{required}")
        entry["match"] = match

    expected_release_orders = set(range(1, len(entries) + 1))
    if release_orders != expected_release_orders:
        raise FranchiseDataError(f"{source}.release_order must be continuous from 1")
    expected_watch_orders = set(range(1, len(watch_orders) + 1))
    if watch_orders != expected_watch_orders:
        raise FranchiseDataError(f"{source}.watch_order must be continuous from 1")

    announcements = definition.get("announcements") or []
    if not isinstance(announcements, list):
        raise FranchiseDataError(f"{source}.announcements must be a list")
    for index, announcement in enumerate(announcements):
        field = f"{source}.announcements[{index}]"
        if not isinstance(announcement, dict):
            raise FranchiseDataError(f"{field} must be an object")
        announcement["title"] = _nonempty_text(announcement.get("title"), f"{field}.title")
        announcement["url"] = _optional_https_url(
            _nonempty_text(announcement.get("url"), f"{field}.url"),
            f"{field}.url",
        )
        for date_key in ("date", "published_at"):
            if announcement.get(date_key) is not None:
                announcement[date_key] = _optional_iso_date(
                    announcement[date_key],
                    f"{field}.{date_key}",
                ).isoformat()
        if announcement.get("official") is not None and type(announcement["official"]) is not bool:
            raise FranchiseDataError(f"{field}.official must be a boolean")

    definition["entries"] = sorted(entries, key=lambda item: item["release_order"])
    definition["announcements"] = sorted(
        announcements,
        key=lambda item: item.get("date") or "",
        reverse=True,
    )
    return definition


def validate_definition_set(definitions, source="franchise definitions"):
    """Reject matchers that could attach one title to multiple entries."""

    matcher_owners = {}
    for definition in definitions:
        for entry in definition["entries"]:
            owner = f"{definition['slug']}/{entry['key']}"
            match = entry.get("match") or {}
            matchers = [
                *(("anidb", str(value)) for value in match.get("anidb") or []),
                *(("shikimori", str(value)) for value in match.get("shikimori") or []),
                *(("source", value) for value in match.get("sources") or []),
            ]
            for matcher in matchers:
                previous = matcher_owners.get(matcher)
                if previous and previous != owner:
                    namespace, value = matcher
                    raise FranchiseDataError(
                        f"{source} matcher {namespace}:{value} belongs to both "
                        f"{previous} and {owner}"
                    )
                matcher_owners[matcher] = owner
    return definitions


def _directory_signature(directory):
    paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
    return tuple((path.name, path.stat().st_mtime_ns, path.stat().st_size) for path in paths), paths


def load_definitions(directory=None):
    directory = Path(directory or DEFAULT_FRANCHISE_DIR)
    signature, paths = _directory_signature(directory)
    key = str(directory.resolve())
    with _CACHE_LOCK:
        cached = _DEFINITION_CACHE.get(key)
        if cached and cached[0] == signature:
            return copy.deepcopy(cached[1])

    definitions = []
    slugs = set()
    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FranchiseDataError(f"Unable to read {path}: {exc}") from exc
        definition = validate_definition(raw, str(path))
        if definition["slug"] in slugs:
            raise FranchiseDataError(f"Duplicate franchise slug {definition['slug']}")
        slugs.add(definition["slug"])
        definitions.append(definition)
    validate_definition_set(definitions, str(directory))
    definitions.sort(key=lambda item: item["title"])
    with _CACHE_LOCK:
        _DEFINITION_CACHE[key] = (signature, copy.deepcopy(definitions))
    return definitions


def reset_cache():
    with _CACHE_LOCK:
        _DEFINITION_CACHE.clear()


def get_definition(slug, directory=None):
    value = str(slug or "").strip()
    return next(
        (definition for definition in load_definitions(directory) if definition["slug"] == value),
        None,
    )


def group_match_context(con, group):
    member_ids = {
        int(value)
        for value in (group.get("source_member_ids") or [group.get("id")])
        if value is not None
    }
    source_keys = set()
    for variant in group.get("source_variants") or [group]:
        source = str(variant.get("source") or "").strip()
        source_id = str(variant.get("source_id") or "").strip()
        if source and source_id:
            source_keys.add(f"{source}:{source_id}")

    refs = {"anidb": set(), "shikimori": set()}
    if member_ids:
        placeholders = ",".join("?" for _ in member_ids)
        rows = con.execute(
            f"""
            select source_ref
            from anime_title_aliases
            where anime_id in ({placeholders})
              and source_ref is not null
            """,
            tuple(sorted(member_ids)),
        ).fetchall()
        for row in rows:
            source_ref = row["source_ref"] if hasattr(row, "keys") else row[0]
            match = EXTERNAL_REF_RE.match(source_ref or "")
            if match:
                refs[match.group(1)].add(match.group(2))
    return {
        "member_ids": member_ids,
        "source_keys": source_keys,
        "external_refs": refs,
    }


def entry_matches_context(entry, context):
    match = entry.get("match") or {}
    source_keys = set(match.get("sources") or [])
    if source_keys & context["source_keys"]:
        return True
    for namespace in ("anidb", "shikimori"):
        expected = {str(value) for value in match.get(namespace) or []}
        if expected & context["external_refs"].get(namespace, set()):
            return True
    return False


def find_for_group(con, group, directory=None):
    context = group_match_context(con, group)
    matches = []
    for definition in load_definitions(directory):
        for entry in definition["entries"]:
            if entry_matches_context(entry, context):
                matches.append((definition, entry))
    if len(matches) > 1:
        labels = ", ".join(
            f"{definition['slug']}/{entry['key']}"
            for definition, entry in matches
        )
        raise FranchiseDataError(
            f"Catalog group {group.get('id')} matches multiple franchise entries: {labels}"
        )
    if matches:
        return matches[0]
    return None, None


def compact_summary(definition):
    entries = definition["entries"]
    years = sorted(
        int(str(entry.get("release_date") or entry.get("release_year") or entry.get("year") or "")[:4])
        for entry in entries
        if str(entry.get("release_date") or entry.get("release_year") or entry.get("year") or "")[:4].isdigit()
    )
    active = any(entry.get("status") in {"releasing", "announced"} for entry in entries)
    year_range = None
    if years:
        year_range = f"{years[0]} — {'сейчас' if active else years[-1]}"
    status_counts = {
        status: sum(1 for entry in entries if entry.get("status") == status)
        for status in ("releasing", "finished", "announced", "unknown")
    }
    return {
        "slug": definition["slug"],
        "title": definition["title"],
        "short_title": definition.get("short_title"),
        "entry_count": len(entries),
        "main_count": sum(1 for entry in entries if entry.get("story_role") == "main"),
        "year_range": year_range,
        "status_counts": status_counts,
        "updated_at": definition.get("updated_at"),
    }
