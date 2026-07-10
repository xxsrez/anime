"""Pure validation and transition rules for per-title library state."""

from __future__ import annotations


WATCH_STATUSES = ("planned", "watching", "paused", "completed", "dropped")
WATCH_STATUS_SET = frozenset(WATCH_STATUSES)
USER_STATE_FIELDS = frozenset(
    {
        "is_favorite",
        "watched",
        "progress_episode_number",
        "watch_status",
        "not_interested",
    }
)


def row_value(row, key, default=None):
    if row is None:
        return default
    if hasattr(row, "keys") and key in row.keys():
        return row[key]
    if isinstance(row, dict):
        return row.get(key, default)
    return default


def inferred_watch_status(*, watched=False, progress_episode_number=None, watch_status=None):
    if watch_status in WATCH_STATUS_SET:
        return watch_status
    if watched:
        return "completed"
    if progress_episode_number is not None:
        return "watching"
    return None


def normalized_state(row=None):
    favorite = bool(row_value(row, "is_favorite", False))
    watched = bool(row_value(row, "watched", False))
    progress = row_value(row, "progress_episode_number")
    status = inferred_watch_status(
        watched=watched,
        progress_episode_number=progress,
        watch_status=row_value(row, "watch_status"),
    )
    return {
        "is_favorite": favorite,
        "progress_episode_number": progress,
        "watched": watched or status == "completed",
        "watch_status": status,
        "not_interested": bool(row_value(row, "not_interested", False)),
        "updated_at": row_value(row, "updated_at"),
        "favorite_updated_at": row_value(row, "favorite_updated_at"),
        "watch_status_updated_at": row_value(row, "watch_status_updated_at"),
        "not_interested_updated_at": row_value(row, "not_interested_updated_at"),
    }


def validate_patch(patch):
    if not isinstance(patch, dict):
        raise ValueError("state patch must be an object")
    unknown = sorted(set(patch) - USER_STATE_FIELDS)
    if unknown:
        raise ValueError(f"unsupported state field: {unknown[0]}")
    if not patch:
        raise ValueError("state patch must contain at least one field")

    validated = {}
    for field in ("is_favorite", "watched", "not_interested"):
        if field in patch:
            if type(patch[field]) is not bool:
                raise ValueError(f"{field} must be a boolean")
            validated[field] = patch[field]

    if "progress_episode_number" in patch:
        value = patch["progress_episode_number"]
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError("progress_episode_number must be a non-negative integer or null")
        validated["progress_episode_number"] = value

    if "watch_status" in patch:
        value = patch["watch_status"]
        if value == "":
            value = None
        if value is not None and value not in WATCH_STATUS_SET:
            raise ValueError("watch_status is invalid")
        validated["watch_status"] = value
    return validated


def apply_patch(current, patch, timestamp):
    """Apply a validated patch and maintain backward-compatible derived fields."""
    patch = validate_patch(patch)
    before = normalized_state(current)
    state = dict(before)

    for field in ("is_favorite", "watched", "progress_episode_number", "watch_status", "not_interested"):
        if field in patch:
            state[field] = patch[field]

    if patch.get("watched") is True:
        state["watch_status"] = "completed"
    elif patch.get("watched") is False and "watch_status" not in patch:
        if before["watch_status"] == "completed":
            state["watch_status"] = "watching" if state["progress_episode_number"] is not None else None

    if "watch_status" in patch:
        status = patch["watch_status"]
        if status == "completed":
            state["watched"] = True
        elif status in {"planned", "watching", "paused", "dropped"} or status is None:
            state["watched"] = False
        if status in {"planned", "dropped"}:
            state["progress_episode_number"] = None

    if "progress_episode_number" in patch and "watch_status" not in patch:
        progress = patch["progress_episode_number"]
        if progress is not None and not state["watched"]:
            state["watch_status"] = "watching"
        elif progress is None and before["watch_status"] == "watching":
            state["watch_status"] = None

    state["updated_at"] = timestamp
    if state["is_favorite"] != before["is_favorite"]:
        state["favorite_updated_at"] = timestamp
    if (
        state["watch_status"] != before["watch_status"]
        or state["watched"] != before["watched"]
        or state["progress_episode_number"] != before["progress_episode_number"]
    ):
        state["watch_status_updated_at"] = timestamp
    if state["not_interested"] != before["not_interested"]:
        state["not_interested_updated_at"] = timestamp
    return state
