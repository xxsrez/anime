"""MyAnimeList identity and rating data for the local catalog."""

import math


FIELDS = ("mal_id", "mal_score", "mal_scored_by")


def positive_id(value):
    text = str(value or "").strip()
    if len(text) > 19 or not text.isascii() or not text.isdecimal():
        return None
    number = int(text)
    return number if 0 < number < 2**63 else None


def score_number(value):
    try:
        score = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if isinstance(value, bool) or not math.isfinite(score) or not 1 <= score <= 10:
        return None
    return round(score, 2)


def ensure_schema(con):
    if con.execute("select 1 from sqlite_master where type='table' and name='anime_mal_ratings'").fetchone():
        return False
    con.execute(
        """
        create table anime_mal_ratings (
            anime_id integer primary key references anime(id) on delete cascade,
            mal_id integer not null check(mal_id > 0),
            score real check(score between 1 and 10),
            scored_by integer check(scored_by > 0),
            checked_at text not null
        )
        """
    )
    return True


def explicit_ids(con):
    ids = {}
    for anime_id, value in con.execute(
        "select anime_id,value from anime_fields where lower(trim(label))='myanimelist id'"
    ):
        if mal_id := positive_id(value):
            ids.setdefault(anime_id, set()).add(mal_id)
    return ids


def load(con):
    ids = explicit_ids(con)
    by_anime = {}
    for anime_id, identifiers in ids.items():
        if len(identifiers) == 1:
            by_anime[anime_id] = {"mal_id": next(iter(identifiers)), "mal_score": None, "mal_scored_by": None}
    for anime_id, value in con.execute(
        "select anime_id,value from anime_fields where lower(trim(label))='myanimelist'"
    ):
        if anime_id in by_anime and (score := score_number(value)) is not None:
            by_anime[anime_id]["mal_score"] = score

    for anime_id, mal_id, score, votes, checked_at in con.execute(
        "select anime_id,mal_id,score,scored_by,checked_at from anime_mal_ratings"
    ):
        if anime_id not in ids or ids[anime_id] == {mal_id}:
            by_anime[anime_id] = {
                "mal_id": mal_id,
                "mal_score": score,
                "mal_scored_by": votes,
                "_mal_checked_at": checked_at,
            }
    return by_anime


def group_rating(items):
    matched = [item for item in items if item.get("mal_id") is not None]
    if len({item["mal_id"] for item in matched}) != 1:
        return dict.fromkeys(FIELDS)
    newest = max(matched, key=lambda item: (item.get("_mal_checked_at", ""), item.get("mal_score") is not None))
    return {key: newest.get(key) for key in FIELDS}
