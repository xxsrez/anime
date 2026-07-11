"""Deterministic, explainable recommendation ranking for catalog dictionaries.

The module deliberately has no database or HTTP dependencies.  Callers pass the
same dictionaries that the catalog API already uses and receive public copies
annotated with recommendation metadata.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
import math
import re
import unicodedata


MODEL_VERSION = "recommendation-v2"

FAVORITE_SEED_WEIGHT = 3.0
WATCHED_SEED_WEIGHT = 0.65
MEANINGFUL_WATCH_SEED_WEIGHT_CAP = 1.2
MEANINGFUL_WATCH_SATURATION_SECONDS = 30 * 60

DEFAULT_LIMIT = 20
DEFAULT_DIVERSITY_WEIGHT = 0.18
DEFAULT_FRANCHISE_CAP = 1

PERSONALIZED_COMPONENT_WEIGHTS = {
    "genre": 0.44,
    "neighbor": 0.22,
    "quality": 0.16,
    "playable": 0.10,
    "popularity": 0.05,
    "kind": 0.03,
}

COLD_START_COMPONENT_WEIGHTS = {
    "quality": 0.55,
    "playable": 0.30,
    "popularity": 0.15,
}

KNOWN_WATCH_STATUSES = frozenset({"watching", "completed"})

_GENRE_ALIASES = {
    "isekai": "исэкай",
    "исекай": "исэкай",
    "исэкай": "исэкай",
}
_GENRE_LABELS = {
    "исэкай": "Исэкай",
}
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
_FRANCHISE_SUFFIX_PATTERNS = (
    re.compile(
        r"\s*[:\-–—]?\s*(?:(?:season|сезон|part|часть)\s*(?:\d+|[ivx]+)"
        r"|(?:\d+|[ivx]+)(?:st|nd|rd|th)?\s*(?:season|сезон|part|часть))\s*$"
    ),
    re.compile(r"\s*[:\-–—]?\s*\d+\s*$"),
    re.compile(
        r"\s*[:\-–—]?\s*(?:ova|ona|special|спешл|(?:the\s+)?movie|film|фильм)\s*$"
    ),
)


def clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def numeric(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _normalize_text(value) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return " ".join(text.split())


def normalize_genre(value) -> str:
    """Return a stable genre key, including known spelling aliases."""

    key = _normalize_text(value)
    return _GENRE_ALIASES.get(key, key)


def genre_label(value) -> str:
    key = normalize_genre(value)
    if key in _GENRE_LABELS:
        return _GENRE_LABELS[key]
    text = str(value or "").strip()
    return text or key


def _genre_label_map(item: Mapping) -> dict[str, str]:
    labels = {}
    for raw_genre in item.get("genres") or ():
        key = normalize_genre(raw_genre)
        if key:
            labels.setdefault(key, genre_label(raw_genre))
    return labels


def _genre_keys(item: Mapping) -> frozenset[str]:
    return frozenset(_genre_label_map(item))


def meaningful_watch_seed_weight(seconds) -> float:
    """Smoothly turn engaged time into a bounded, deliberately weak signal."""

    seconds = max(0.0, numeric(seconds) or 0.0)
    if seconds == 0:
        return 0.0
    weight = MEANINGFUL_WATCH_SEED_WEIGHT_CAP * (
        1.0 - math.exp(-seconds / MEANINGFUL_WATCH_SATURATION_SECONDS)
    )
    return min(MEANINGFUL_WATCH_SEED_WEIGHT_CAP, weight)


def _watch_status(item: Mapping) -> str | None:
    if item.get("watched"):
        return "completed"
    for key in ("watch_status", "user_watch_status", "library_status"):
        value = _normalize_text(item.get(key))
        if value:
            return {
                "paused": "watching",
                "planned": "none",
                "dropped": "none",
            }.get(value, value if value in {"none", "watching", "completed"} else None)
    if item.get("progress_episode_number") is not None:
        return "watching"
    return "none"


def _meaningful_watch_seconds(item: Mapping) -> float:
    if "meaningful_watch_seconds" in item:
        return max(0.0, numeric(item.get("meaningful_watch_seconds")) or 0.0)
    return max(0.0, numeric(item.get("watch_engaged_seconds")) or 0.0)


def seed_weight(item: Mapping) -> float:
    """Return positive-evidence strength without treating completion as a like."""

    status = _watch_status(item)
    if item.get("not_interested"):
        return 0.0
    if item.get("is_favorite"):
        return FAVORITE_SEED_WEIGHT

    explicit_watched = bool(item.get("watched")) or status == "completed"
    watched_weight = WATCHED_SEED_WEIGHT if explicit_watched else 0.0
    engagement_weight = meaningful_watch_seed_weight(_meaningful_watch_seconds(item))
    return max(watched_weight, engagement_weight)


def is_known_item(item: Mapping) -> bool:
    """Whether an item belongs to the user's library/history, not discovery."""

    if item.get("not_interested"):
        return True
    if _watch_status(item) in KNOWN_WATCH_STATUSES:
        return True
    if item.get("is_favorite") or item.get("watched"):
        return True
    if item.get("progress_episode_number") is not None:
        return True
    return seed_weight(item) > 0.0


def _catalog_genre_statistics(items: Sequence[Mapping]):
    document_frequency = Counter()
    labels = {}
    for item in items:
        label_map = _genre_label_map(item)
        document_frequency.update(label_map.keys())
        for key, label in label_map.items():
            labels.setdefault(key, label)

    document_count = max(1, len(items))
    idf = {
        key: math.log((document_count + 1.0) / (frequency + 1.0)) + 1.0
        for key, frequency in document_frequency.items()
    }
    max_idf = max(idf.values(), default=1.0)
    specificity = {key: value / max_idf for key, value in idf.items()}
    return idf, specificity, labels


def _build_profile(items: Sequence[Mapping]):
    idf, specificity, catalog_labels = _catalog_genre_statistics(items)
    seeds = []
    genre_weights = Counter()
    kind_weights = Counter()

    for item in items:
        weight = seed_weight(item)
        if weight <= 0:
            continue
        keys = _genre_keys(item)
        seed = {
            "item": item,
            "weight": weight,
            "genre_keys": keys,
        }
        seeds.append(seed)

        total_specificity = sum(idf.get(key, 1.0) for key in keys)
        if total_specificity:
            for key in keys:
                # Each seed contributes the same total evidence regardless of
                # how many genre labels the source happens to contain.
                genre_weights[key] += weight * idf.get(key, 1.0) / total_specificity

        kind = _normalize_text(item.get("kind"))
        if kind:
            kind_weights[kind] += weight

    top_genres = []
    for key, weight in sorted(
        genre_weights.items(),
        key=lambda pair: (-pair[1], catalog_labels.get(pair[0], pair[0])),
    )[:8]:
        top_genres.append(
            {
                "genre": catalog_labels.get(key, _GENRE_LABELS.get(key, key)),
                "weight": round(weight, 3),
                "specificity": round(specificity.get(key, 1.0), 3),
            }
        )

    favorite_count = sum(1 for item in items if item.get("is_favorite"))
    mode = "personalized" if seeds else "cold_start"
    return {
        "model_version": MODEL_VERSION,
        "mode": mode,
        "confidence_available": bool(seeds),
        "favorite_count": favorite_count,
        "seed_count": len(seeds),
        "top_genres": top_genres,
        "_seeds": seeds,
        "_genre_weights": dict(genre_weights),
        "_kind_weights": dict(kind_weights),
        "_idf": idf,
        "_specificity": specificity,
        "_catalog_labels": catalog_labels,
    }


def _public_profile(profile: Mapping) -> dict:
    return {key: value for key, value in profile.items() if not key.startswith("_")}


def build_recommendation_profile(items: Iterable[Mapping]) -> dict:
    """Build a JSON-friendly taste profile without ranking candidates."""

    catalog = sorted(
        (dict(item) for item in items if isinstance(item, Mapping)),
        key=_stable_item_key,
    )
    return _public_profile(_build_profile(catalog))


def _weighted_jaccard(left: set[str] | frozenset[str], right, idf: Mapping) -> float:
    union = left | right
    if not union:
        return 0.0
    intersection_weight = sum(idf.get(key, 1.0) for key in left & right)
    union_weight = sum(idf.get(key, 1.0) for key in union)
    return clamp(intersection_weight / union_weight) if union_weight else 0.0


def _genre_profile_score(candidate: Mapping, profile: Mapping):
    keys = _genre_keys(candidate)
    genre_weights = profile["_genre_weights"]
    if not keys or not genre_weights:
        return 0.0, []

    idf = profile["_idf"]
    profile_norm = math.sqrt(sum(value * value for value in genre_weights.values()))
    candidate_vector = {key: idf.get(key, 1.0) for key in keys}
    candidate_norm = math.sqrt(sum(value * value for value in candidate_vector.values()))
    dot_product = sum(
        genre_weights.get(key, 0.0) * candidate_vector[key] for key in keys
    )
    score = dot_product / (profile_norm * candidate_norm) if profile_norm and candidate_norm else 0.0

    labels = _genre_label_map(candidate)
    matched_keys = sorted(
        (key for key in keys if key in genre_weights),
        key=lambda key: (
            -(genre_weights[key] * candidate_vector[key]),
            labels.get(key, key),
        ),
    )
    return clamp(score), [labels.get(key, key) for key in matched_keys]


def _nearest_seed_score(candidate: Mapping, profile: Mapping):
    candidate_keys = _genre_keys(candidate)
    if not candidate_keys:
        return 0.0, []

    idf = profile["_idf"]
    candidate_labels = _genre_label_map(candidate)
    matches = []
    for seed in profile["_seeds"]:
        seed_keys = seed["genre_keys"]
        if not seed_keys:
            continue
        similarity = _weighted_jaccard(candidate_keys, seed_keys, idf)
        if similarity <= 0:
            continue
        strength = clamp(seed["weight"] / FAVORITE_SEED_WEIGHT)
        weighted_score = similarity * strength
        seed_item = seed["item"]
        matches.append(
            {
                "id": seed_item.get("id"),
                "title": seed_item.get("title") or "Без названия",
                "score": round(weighted_score, 3),
                "similarity": round(similarity, 3),
                "seed_weight": round(seed["weight"], 3),
                "matched_genres": [
                    candidate_labels.get(key, key)
                    for key in sorted(
                        candidate_keys & seed_keys,
                        key=lambda key: candidate_labels.get(key, key),
                    )
                ],
            }
        )

    matches.sort(
        key=lambda match: (
            -match["score"],
            -match["similarity"],
            _normalize_text(match["title"]),
            str(match["id"]),
        )
    )
    return (matches[0]["score"] if matches else 0.0), matches[:2]


def _kind_profile_score(item: Mapping, profile: Mapping) -> float:
    kind = _normalize_text(item.get("kind"))
    weights = profile["_kind_weights"]
    if not kind or not weights:
        return 0.0
    return clamp(weights.get(kind, 0.0) / max(weights.values()))


def _best_rating(item: Mapping):
    keys = []
    if _normalize_text(item.get("effective_score_source")) != "synthetic":
        keys.append("effective_score")
    keys.extend(("external_score", "aggregate_score", "listing_score"))
    for key in keys:
        value = numeric(item.get(key))
        if value is not None:
            if 10 < value <= 100:
                value /= 10.0
            return clamp(value, 0.0, 10.0)
    return None


def _quality_score(item: Mapping) -> float:
    rating = _best_rating(item)
    if rating is None:
        return 0.35
    return clamp((rating - 5.0) / 5.0)


def _popularity_score(item: Mapping) -> float:
    count = 0.0
    for key in ("aggregate_count", "popularity", "external_popularity"):
        count = max(count, numeric(item.get(key)) or 0.0)
    return clamp(math.log10(count + 1.0) / 5.0) if count > 0 else 0.0


def has_playable_source(item: Mapping) -> bool:
    if "has_video" in item and bool(item.get("has_video")):
        return True
    return (
        (numeric(item.get("source_count")) or 0) > 0
        or (numeric(item.get("available_episode_count")) or 0) > 0
    )


def _year_number(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    match = _YEAR_RE.search(str(value or ""))
    return int(match.group(0)) if match else None


def _choice_values(value) -> tuple:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        return (value,)
    return tuple(value)


def _matches_choice(actual, requested) -> bool:
    actual_key = _normalize_text(actual)
    return any(actual_key == _normalize_text(value) for value in _choice_values(requested))


def _matches_genre_filter(item: Mapping, requested) -> bool:
    available = _genre_keys(item)
    requested_keys = {
        normalize_genre(value) for value in _choice_values(requested) if normalize_genre(value)
    }
    return bool(available & requested_keys)


def _source_values(item: Mapping) -> set[str]:
    values = set()
    for value in (item.get("source"), *(item.get("sources") or ())):
        key = _normalize_text(value)
        if key:
            values.add(key)
    for variant in item.get("source_variants") or ():
        if isinstance(variant, Mapping):
            key = _normalize_text(variant.get("source"))
            if key:
                values.add(key)
    return values


def _matches_filters(item: Mapping, filters: Mapping | None) -> bool:
    if not filters:
        return True

    genre = filters.get("genre", filters.get("genres"))
    if genre not in (None, "") and not _matches_genre_filter(item, genre):
        return False

    if filters.get("year") not in (None, ""):
        requested_years = {
            _year_number(value) for value in _choice_values(filters["year"])
        }
        if _year_number(item.get("year")) not in requested_years:
            return False

    year_min = filters.get("year_min", filters.get("year_from"))
    year_max = filters.get("year_max", filters.get("year_to"))
    item_year = _year_number(item.get("year"))
    if year_min not in (None, ""):
        minimum = _year_number(year_min)
        if minimum is not None and (item_year is None or item_year < minimum):
            return False
    if year_max not in (None, ""):
        maximum = _year_number(year_max)
        if maximum is not None and (item_year is None or item_year > maximum):
            return False

    for key in ("kind", "status"):
        requested = filters.get(key)
        if requested not in (None, "") and not _matches_choice(item.get(key), requested):
            return False

    requested_source = filters.get("source")
    if requested_source not in (None, ""):
        requested_sources = {
            _normalize_text(value) for value in _choice_values(requested_source)
        }
        if not (_source_values(item) & requested_sources):
            return False

    video = filters.get("video", filters.get("has_video"))
    if video is not None and _boolean_filter_value(video) != has_playable_source(item):
        return False
    return True


def _boolean_filter_value(value) -> bool:
    if isinstance(value, str):
        key = _normalize_text(value)
        if key in {"", "0", "false", "no", "нет", "off"}:
            return False
        if key in {"1", "true", "yes", "да", "on"}:
            return True
    return bool(value)


def filter_catalog_items(
    items: Iterable[Mapping],
    *,
    filters: Mapping | None = None,
    predicates: Callable[[Mapping], bool] | Iterable[Callable[[Mapping], bool]] | None = None,
) -> list[dict]:
    """Apply discovery filters before scoring, pooling, and top-K selection."""

    if predicates is None:
        predicate_list = ()
    elif callable(predicates):
        predicate_list = (predicates,)
    else:
        predicate_list = tuple(predicates)

    result = []
    for item in items:
        if not isinstance(item, Mapping) or not _matches_filters(item, filters):
            continue
        if all(predicate(item) for predicate in predicate_list):
            result.append(dict(item))
    return result


def _stable_item_key(item: Mapping):
    return (
        _normalize_text(item.get("title") or item.get("subtitle") or ""),
        str(item.get("id") if item.get("id") is not None else ""),
    )


def _franchise_key(item: Mapping):
    for key in (
        "franchise_id",
        "franchise_key",
        "franchise",
        "relation_group_id",
        "canonical_franchise_id",
        "series_id",
    ):
        value = item.get(key)
        if isinstance(value, Mapping):
            value = value.get("id", value.get("key", value.get("title")))
        if value not in (None, ""):
            return ("franchise", _normalize_text(value))

    # The current catalog has no relation graph yet.  A conservative fallback
    # groups exact title families after removing explicit season/part suffixes.
    # Prefer the romanized subtitle: it tends to stay stable across sources.
    title = _normalize_text(item.get("subtitle") or item.get("title"))
    base = title
    while base:
        changed = False
        for pattern in _FRANCHISE_SUFFIX_PATTERNS:
            stripped = pattern.sub("", base).strip(" ,:-–—")
            if stripped != base:
                base = stripped
                changed = True
                break
        if not changed:
            break
    if ":" in base:
        prefix = base.split(":", 1)[0].strip()
        if len(prefix) >= 4:
            base = prefix
    if base:
        return ("derived-title", base)
    return ("item",) + _stable_item_key(item)


def _base_components(item: Mapping, profile: Mapping):
    genre, matched_genres = _genre_profile_score(item, profile)
    neighbor, based_on = _nearest_seed_score(item, profile)
    components = {
        "genre": genre,
        "neighbor": neighbor,
        "kind": _kind_profile_score(item, profile),
        "quality": _quality_score(item),
        "playable": 1.0 if has_playable_source(item) else 0.0,
        "popularity": _popularity_score(item),
    }
    weights = (
        PERSONALIZED_COMPONENT_WEIGHTS
        if profile["mode"] == "personalized"
        else COLD_START_COMPONENT_WEIGHTS
    )
    base_score = sum(components[key] * weight for key, weight in weights.items())
    return components, clamp(base_score), matched_genres, based_on


def _format_rating(item: Mapping) -> str | None:
    rating = _best_rating(item)
    if rating is None:
        return None
    return f"{rating:.1f}".rstrip("0").rstrip(".")


def _recommendation_reasons(
    item: Mapping,
    *,
    mode: str,
    matched_genres: Sequence[str],
    based_on: Sequence[Mapping],
) -> list[str]:
    reasons = []
    if mode == "cold_start":
        reasons.append("Стартовый выбор: пока мало данных о ваших вкусах")
    else:
        if matched_genres:
            reasons.append(f"Совпали важные жанры: {', '.join(matched_genres[:4])}")
        if based_on:
            titles = ", ".join(str(match["title"]) for match in based_on[:2])
            reasons.append(f"Похоже на: {titles}")

    rating = _format_rating(item)
    if rating:
        reasons.append(f"Рейтинг {rating}/10")
    if has_playable_source(item):
        reasons.append("Есть видео в каталоге")
    else:
        reasons.append("Пока без видео в каталоге")
    return reasons[:4]


def _score_candidates(candidates: Sequence[Mapping], profile: Mapping):
    scored = []
    for item in candidates:
        components, base_score, matched_genres, based_on = _base_components(item, profile)
        scored.append(
            {
                "item": dict(item),
                "genre_keys": _genre_keys(item),
                "components": components,
                "base_score": base_score,
                "matched_genres": matched_genres,
                "based_on": based_on,
            }
        )
    scored.sort(
        key=lambda entry: (
            -entry["base_score"],
            _stable_item_key(entry["item"]),
        )
    )
    return scored


def _diversity_rerank(
    scored: Sequence[Mapping],
    *,
    limit: int,
    pool_size: int,
    idf: Mapping,
    diversity_weight: float,
    franchise_cap: int | None,
):
    remaining = list(scored[:pool_size])
    selected = []
    franchise_counts = Counter()

    while remaining and len(selected) < limit:
        choices = []
        for entry in remaining:
            franchise = _franchise_key(entry["item"])
            if franchise_cap is not None and franchise_counts[franchise] >= franchise_cap:
                continue
            similarity = max(
                (
                    _weighted_jaccard(entry["genre_keys"], chosen["genre_keys"], idf)
                    for chosen in selected
                ),
                default=0.0,
            )
            penalty = diversity_weight * similarity
            rerank_score = clamp(entry["base_score"] - penalty)
            choices.append(
                (
                    -rerank_score,
                    -entry["base_score"],
                    _stable_item_key(entry["item"]),
                    entry,
                    penalty,
                    rerank_score,
                    franchise,
                )
            )

        if not choices:
            break
        choices.sort(key=lambda choice: choice[:3])
        _, _, _, chosen, penalty, rerank_score, franchise = choices[0]
        remaining.remove(chosen)
        selected.append(
            {
                **chosen,
                "diversity_penalty": penalty,
                "rerank_score": rerank_score,
            }
        )
        franchise_counts[franchise] += 1
    return selected


def rank_recommendations(
    catalog_items: Iterable[Mapping],
    *,
    limit: int = DEFAULT_LIMIT,
    filters: Mapping | None = None,
    predicates: Callable[[Mapping], bool] | Iterable[Callable[[Mapping], bool]] | None = None,
    pool_size: int | None = None,
    diversity_weight: float = DEFAULT_DIVERSITY_WEIGHT,
    franchise_cap: int | None = DEFAULT_FRANCHISE_CAP,
) -> dict:
    """Rank catalog dictionaries and return ``{"items": ..., "profile": ...}``.

    Catalog filters and custom predicates run before candidates enter the score
    pool.  Positive/known library items still contribute to the taste profile,
    but are never recommended back to the user.
    """

    catalog = sorted(
        (dict(item) for item in catalog_items if isinstance(item, Mapping)),
        key=_stable_item_key,
    )
    profile = _build_profile(catalog)

    try:
        normalized_limit = max(0, int(limit))
    except (TypeError, ValueError):
        normalized_limit = DEFAULT_LIMIT
    diversity_weight = clamp(numeric(diversity_weight) or 0.0)
    if franchise_cap is not None:
        franchise_cap = max(1, int(franchise_cap))

    unknown_items = [item for item in catalog if not is_known_item(item)]
    candidates = filter_catalog_items(
        unknown_items,
        filters=filters,
        predicates=predicates,
    )
    scored = _score_candidates(candidates, profile)

    if pool_size is None:
        normalized_pool_size = max(normalized_limit * 5, normalized_limit, 50)
    else:
        normalized_pool_size = max(normalized_limit, int(pool_size))
    normalized_pool_size = min(len(scored), normalized_pool_size)

    selected = _diversity_rerank(
        scored,
        limit=normalized_limit,
        pool_size=normalized_pool_size,
        idf=profile["_idf"],
        diversity_weight=diversity_weight,
        franchise_cap=franchise_cap,
    )

    ranked_items = []
    for rank, entry in enumerate(selected, start=1):
        item = dict(entry["item"])
        components = {
            key: round(clamp(value), 4)
            for key, value in entry["components"].items()
        }
        # Keep the v1 public key as an alias while v2 uses the more precise
        # `playable` name internally.
        components["watchable"] = components["playable"]
        components.update(
            {
                "base_score": round(entry["base_score"], 4),
                "rerank_score": round(entry["rerank_score"], 4),
                "diversity_penalty": round(entry["diversity_penalty"], 4),
                "model_version": MODEL_VERSION,
            }
        )
        item.update(
            {
                "recommendation_rank": rank,
                "recommendation_score": round(entry["rerank_score"] * 100.0, 1),
                "recommendation_base_score": round(entry["base_score"] * 100.0, 1),
                "recommendation_confidence": None,
                "recommendation_model_version": MODEL_VERSION,
                "recommendation_matched_genres": entry["matched_genres"][:6],
                "recommendation_based_on": entry["based_on"],
                "recommendation_reasons": _recommendation_reasons(
                    item,
                    mode=profile["mode"],
                    matched_genres=entry["matched_genres"],
                    based_on=entry["based_on"],
                ),
                "recommendation_components": components,
            }
        )
        ranked_items.append(item)

    public_profile = _public_profile(profile)
    public_profile.update(
        {
            "candidate_count": len(unknown_items),
            "filtered_candidate_count": len(candidates),
            "returned_count": len(ranked_items),
        }
    )
    return {
        "items": ranked_items,
        "limit": normalized_limit,
        "profile": public_profile,
        "model_version": MODEL_VERSION,
    }


__all__ = [
    "COLD_START_COMPONENT_WEIGHTS",
    "DEFAULT_DIVERSITY_WEIGHT",
    "DEFAULT_FRANCHISE_CAP",
    "FAVORITE_SEED_WEIGHT",
    "MEANINGFUL_WATCH_SEED_WEIGHT_CAP",
    "MODEL_VERSION",
    "PERSONALIZED_COMPONENT_WEIGHTS",
    "WATCHED_SEED_WEIGHT",
    "build_recommendation_profile",
    "filter_catalog_items",
    "genre_label",
    "has_playable_source",
    "is_known_item",
    "meaningful_watch_seed_weight",
    "normalize_genre",
    "rank_recommendations",
    "seed_weight",
]
