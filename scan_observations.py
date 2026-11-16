"""Conservative source-level evidence for scanner selection, not release status."""

import calendar
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3


SCHEMA_FILE = Path(__file__).resolve().parent / "migrations/2026-11-16_scan-observations/00_scan_observations.sql"
COMPLETED = {"вышел", "завершён", "завершен", "выпуск завершён", "выпуск завершен", "finished", "completed"}
ONGOING = {"онгоинг", "ongoing", "выходит", "анонс", "announced"}


def ensure_schema(con):
    statement = []
    for char in SCHEMA_FILE.read_text(encoding="utf-8"):
        statement.append(char)
        if char == ";" and sqlite3.complete_statement("".join(statement)):
            con.execute("".join(statement))
            statement.clear()
    if "".join(statement).strip():
        raise ValueError("incomplete scan observation schema")


def one_month_after(value):
    year, month = value.year, value.month + 1
    if month == 13:
        year, month = year + 1, 1
    return value.replace(year=year, month=month, day=min(value.day, calendar.monthrange(year, month)[1]))


def parse_time(value):
    try:
        result = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (result if result.tzinfo else result.replace(tzinfo=dt.timezone.utc)).astimezone(dt.timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def identity(row):
    return json.dumps([row["source"], row["source_id"], row["url"]], ensure_ascii=False, separators=(",", ":"))


def fingerprint(episodes):
    # Stable upstream identities, not URLs (which may contain rotating tokens).
    content = []
    for snapshot in episodes:
        episode = snapshot["episode"]
        providers = sorted((str(p["provider_id"]), int(p["translation_id"]), str(p["embed_host"]).lower())
                           for p in snapshot["providers"])
        content.append((int(episode["id"]), str(episode.get("number") or ""), providers))
    data = json.dumps(["v1", sorted(content)], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def invalidate(con, anime_id):
    con.execute("update scan_source_observations set last_attempt_complete=0 where anime_id=?", (anime_id,))


def record(con, anime_row, episodes, checked_at, *, complete, started_at=None, job_id=None, user_id=None):
    anime_id = int(anime_row["id"])
    if not complete or not episodes:
        invalidate(con, anime_id)
        return
    timestamp = checked_at.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    evidence_from = parse_time(started_at) if started_at is not None else checked_at
    if evidence_from is None or evidence_from > checked_at:
        invalidate(con, anime_id)
        return
    evidence_from = evidence_from.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    current_identity = identity(anime_row)
    current_fingerprint = fingerprint(episodes)
    event_id = con.execute("select coalesce(max(id),0) from content_update_events where anime_id=? and event_type!='new_title'", (anime_id,)).fetchone()[0]
    old = con.execute("select * from scan_source_observations where anime_id=?", (anime_id,)).fetchone()
    since = timestamp
    if old and old["source_identity"] == current_identity and old["content_fingerprint"] == current_fingerprint:
        since = old["unchanged_since"]
        # Changes inserted by a background sync since our last observation must
        # restart the period, even when a later snapshot matches the baseline.
        last = parse_time(old["last_complete_at"])
        changed = con.execute("select max(julianday(occurred_at)) from content_update_events where anime_id=? and event_type!='new_title' and julianday(occurred_at)>julianday(?)", (anime_id, old["last_complete_at"])).fetchone()[0]
        catalog_change = con.execute("select 1 from title_content_activity where anime_id=? and (julianday(last_changed_at)>julianday(?) or julianday(source_updated_at)>julianday(?))", (anime_id, old["last_complete_at"], old["last_complete_at"])).fetchone()
        if last is None or event_id > old["last_event_id"] or changed is not None or catalog_change is not None:
            since = timestamp
    con.execute("""insert into scan_source_observations
        (anime_id,source_identity,content_fingerprint,unchanged_since,last_complete_at,last_evidence_from,last_event_id,last_job_id,last_user_id,last_attempt_complete)
        values (?,?,?,?,?,?,?,?,?,1) on conflict(anime_id) do update set
        source_identity=excluded.source_identity, content_fingerprint=excluded.content_fingerprint,
        unchanged_since=excluded.unchanged_since, last_complete_at=excluded.last_complete_at,
        last_evidence_from=excluded.last_evidence_from, last_event_id=excluded.last_event_id, last_job_id=excluded.last_job_id,
        last_user_id=excluded.last_user_id, last_attempt_complete=1""",
        (anime_id, current_identity, current_fingerprint, since, timestamp, evidence_from, event_id, job_id, user_id))


def inactive_by_observation(row, observation, now):
    if not observation or not observation["last_attempt_complete"] or observation["source_identity"] != identity(row):
        return False
    since, checked = parse_time(observation["unchanged_since"]), parse_time(observation["last_complete_at"])
    evidence_from = parse_time(observation["last_evidence_from"])
    if since is None or checked is None or evidence_from is None or one_month_after(since) > evidence_from or checked > now:
        return False
    if int(row["last_content_event_id"] or 0) > int(observation["last_event_id"] or 0):
        return False
    for key in ("last_content_change_at", "last_scan_change_at", "last_observed_change_at", "last_source_update_at"):
        changed = parse_time(row[key])
        if changed is not None and changed > checked:
            return False
    return True


def completed_group(rows, observations, now):
    statuses = {str(row["status"] or "").strip().casefold() for row in rows}
    active_status = any(status in ONGOING or "онго" in status for status in statuses)
    if statuses & COMPLETED and not active_status:
        return True
    return bool(rows) and all(inactive_by_observation(row, observations.get(int(row["id"])), now) for row in rows)
