#!/usr/bin/env python3
"""User-powered, additive-only AnimeGO scan jobs."""

import datetime as dt
import hashlib
import hmac
import io
import json
import math
import random
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse
import zipfile
from urllib.request import Request

import content_updates
import scrape_yummyanime as yummy
import scrape_animego as animego
import kinogoo
import scan_observations
import title_activity
from scripts.operation_lock import DatabaseOperationLock, OperationLockError
from scripts.http_safety import open_validated_url


SCAN_MODES = {"partial", "extended", "full"}
ACTIVE_JOB_STATUS = "running"
JOB_TTL_SECONDS = 24 * 60 * 60
PARTIAL_EXTRA_LIMIT = 8
SCAN_RECENCY_HOURS = 12
MAX_RESULT_BODY_BYTES = 2 * 1024 * 1024
MAX_COMPLETE_BODY_BYTES = 256 * 1024
MAX_RESULT_EPISODES = 2_000
MAX_PROVIDERS_PER_EPISODE = 100
MAX_RESULT_PROVIDERS = 5_000
MAX_ERROR_TEXT = 2_000
MAX_PROVIDER_URL = 8_192
ONGOING_STATUSES = {"ongoing", "онгоинг"}
SOURCE_FETCH_LIMIT = threading.BoundedSemaphore(1)
YUMMY_DISCOVERY_PAGE_SIZE = 100
MAX_YUMMY_DISCOVERY_PAGES = 500
UNREADABLE_CARD = "Не удалось прочитать карточку AnimeGO."


class ScanError(RuntimeError):
    pass


class ScanConflictError(ScanError):
    def __init__(self, job):
        super().__init__("scan already in progress")
        self.job = job


class ScanAuthenticationError(ScanError):
    pass


class ScanExpiredError(ScanError):
    pass


class ScanOperationBusyError(ScanError):
    pass


class ScanSourceUnavailable(ScanError):
    pass


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def iso_timestamp(value=None):
    return (value or now_utc()).isoformat(timespec="seconds")


def supports_catalog_sources(version):
    if not isinstance(version, str) or not re.fullmatch(r"\d{1,4}\.\d{1,4}\.\d{1,4}", version):
        return False
    return tuple(int(component) for component in version.split(".")) >= (0, 5, 17)


def hash_token(token):
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def connect(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("pragma busy_timeout=30000")
    con.execute("pragma foreign_keys=on")
    return con


def ensure_schema(con):
    existed = bool(
        con.execute(
            "select 1 from sqlite_master where type = 'table' and name = 'animego_scan_jobs'"
        ).fetchone()
    )
    con.executescript(
        """
        create table if not exists animego_scan_discovery (
            job_id integer not null references animego_scan_jobs(id) on delete cascade,
            anime_id integer not null,
            page integer not null,
            item_json text not null,
            processed integer not null default 0,
            primary key (job_id, anime_id)
        );
        create table if not exists animego_scan_pages (
            job_id integer not null references animego_scan_jobs(id) on delete cascade,
            page integer not null,
            done integer not null,
            fingerprint text,
            primary key (job_id, page)
        );
        create table if not exists animego_scan_jobs (
            id integer primary key autoincrement,
            user_id integer not null references users(id) on delete restrict,
            user_email text,
            user_name text,
            mode text not null check (mode in ('partial', 'full')),
            status text not null check (status in ('running', 'completed', 'stopped', 'expired')),
            token_hash text not null unique,
            current_anime_id integer references anime(id) on delete set null,
            content_update_run_id integer references content_update_runs(id) on delete set null,
            created_at text not null,
            expires_at text not null,
            completed_at text,
            total_items integer not null default 0,
            checked_items integer not null default 0,
            new_episode_count integer not null default 0,
            new_provider_count integer not null default 0,
            error_count integer not null default 0,
            last_error text
        );

        create unique index if not exists idx_animego_scan_jobs_one_running
            on animego_scan_jobs(status)
            where status = 'running';
        create index if not exists idx_animego_scan_jobs_user_created
            on animego_scan_jobs(user_id, created_at desc);

        create table if not exists animego_scan_job_modes (
            job_id integer primary key references animego_scan_jobs(id) on delete cascade,
            mode text not null check (mode in ('extended', 'full_v2'))
        );

        create table if not exists animego_scan_job_items (
            job_id integer not null references animego_scan_jobs(id) on delete cascade,
            anime_id integer not null references anime(id) on delete cascade,
            position integer not null,
            selection_reason text not null,
            status text not null default 'pending'
                check (status in ('pending', 'completed', 'failed')),
            checked_at text,
            new_episode_count integer not null default 0,
            new_provider_count integer not null default 0,
            error text,
            primary key (job_id, anime_id),
            unique (job_id, position)
        );

        create index if not exists idx_animego_scan_job_items_status
            on animego_scan_job_items(job_id, status, position);

        create table if not exists animego_title_scan_state (
            anime_id integer primary key references anime(id) on delete cascade,
            last_checked_at text,
            last_changed_at text,
            next_eligible_at text,
            consecutive_no_change integer not null default 0,
            last_error text,
            last_checked_by_user_id integer references users(id) on delete set null,
            last_scan_job_id integer references animego_scan_jobs(id) on delete set null
        );

        create index if not exists idx_animego_title_scan_state_eligible
            on animego_title_scan_state(next_eligible_at, last_checked_at);

        create table if not exists animego_episode_additions (
            id integer primary key autoincrement,
            source_episode_id integer not null unique,
            anime_id integer not null references anime(id) on delete cascade,
            episode_id integer references episodes(id) on delete set null,
            user_id integer not null references users(id) on delete restrict,
            scan_job_id integer not null references animego_scan_jobs(id) on delete restrict,
            added_at text not null,
            payload_hash text not null,
            reverted_at text
        );

        create index if not exists idx_animego_episode_additions_user
            on animego_episode_additions(user_id, added_at desc);
        create index if not exists idx_animego_episode_additions_job
            on animego_episode_additions(scan_job_id, added_at);

        create table if not exists animego_provider_additions (
            id integer primary key autoincrement,
            video_source_id integer references video_sources(id) on delete set null,
            anime_id integer not null references anime(id) on delete cascade,
            source_episode_id integer not null,
            episode_id integer references episodes(id) on delete set null,
            provider_key text not null,
            user_id integer not null references users(id) on delete restrict,
            scan_job_id integer not null references animego_scan_jobs(id) on delete restrict,
            added_at text not null,
            payload_hash text not null,
            reverted_at text,
            unique (episode_id, provider_key)
        );

        create index if not exists idx_animego_provider_additions_user
            on animego_provider_additions(user_id, added_at desc);
        create index if not exists idx_animego_provider_additions_job
            on animego_provider_additions(scan_job_id, added_at);
        """
    )
    scan_observations.ensure_schema(con)
    return not existed


def parsed_episode_total(value):
    match = re.search(r"\d+", str(value or ""))
    return int(match.group(0)) if match else None


def title_is_ongoing(row):
    status = str(row["status"] if isinstance(row, sqlite3.Row) else row.get("status") or "")
    folded = status.strip().casefold()
    return folded in ONGOING_STATUSES or "онго" in folded


def title_needs_player_backfill(row):
    available = int(row["available_episode_count"] or 0)
    episode_count = int(row["episode_count"] or 0)
    expected = parsed_episode_total(row["episodes_text"])
    return available == 0 or episode_count > available or (
        expected is not None and expected > available
    )


def completed_over_month_ago(row, now):
    if str(row["status"] or "").strip().casefold() not in {
        "вышел", "завершён", "завершен", "выпуск завершён", "выпуск завершен",
    }:
        return False
    # A release period expressed in years has a conservative upper bound:
    # December 31 of its final year. Do not substitute the premiere year column.
    release = str(row["release_period"] or "").strip()
    years = re.fullmatch(r"(\d{4})(?:\s*[-–—]\s*(\d{4}))?", release)
    if years:
        first, last = int(years[1]), int(years[2] or years[1])
        if not 1 <= first <= last <= 9999:
            return False
        return (now.date() - dt.date(last, 12, 31)).days > 30
    match = re.search(r"\bпо\s+(\d{1,2})\s+(\w+)\s+(\d{4})", release)
    # A standalone date on films and specials is their release date. On a
    # series it can be only the premiere, so retain series without an end date.
    if not match and str(row["kind"] or "").strip().casefold() in {"фильм", "спешл", "спецвыпуск"}:
        match = re.fullmatch(r"(\d{1,2})\s+(\w+)\s+(\d{4})", release)
    months = "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split()
    if not match or match[2].casefold() not in months:
        return False
    try:
        ended = dt.date(int(match[3]), months.index(match[2].casefold()) + 1, int(match[1]))
    except ValueError:
        return False
    return (now.date() - ended).days > 30


def scan_candidate_rows(con, *, include_completed=False, include_all_sources=False):
    rows = con.execute(
        """
        with episode_counts as (
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
        )
        select
            a.*,
            (select value from anime_fields f where f.anime_id = a.id
             and f.label = 'Выпуск') as release_period,
            (select max(occurred_at) from content_update_events e
             where e.anime_id = a.id and e.event_type != 'new_title') as last_content_change_at,
            (select coalesce(max(id), 0) from content_update_events e
             where e.anime_id = a.id and e.event_type != 'new_title') as last_content_event_id,
            (select occurred_at from content_update_events e
             where e.anime_id = a.id and julianday(e.occurred_at) is not null
             order by julianday(e.occurred_at) desc limit 1) as last_catalog_change_at,
            scan_state.last_changed_at as last_scan_change_at,
            activity.last_changed_at as last_observed_change_at,
            activity.source_updated_at as last_source_update_at,
            coalesce(ec.episode_count, 0) as episode_count,
            coalesce(aec.available_episode_count, 0) as available_episode_count
        from anime a
        left join episode_counts ec on ec.anime_id = a.id
        left join available_episode_counts aec on aec.anime_id = a.id
        left join animego_title_scan_state scan_state on scan_state.anime_id = a.id
        left join title_content_activity activity on activity.anime_id = a.id
        where ? or a.source in ('animego', 'kinogoo')
        order by cast(coalesce(a.year, '0') as integer) desc, a.id desc
        """, (int(include_all_sources),)
    ).fetchall()
    return rows if include_completed else [row for row in rows if row['source'] == 'kinogoo' or title_is_ongoing(row) or title_needs_player_backfill(row)]


def scan_state_by_anime_id(con, anime_ids):
    if not anime_ids:
        return {}
    states = {}
    for start in range(0, len(anime_ids), 900):
        chunk = anime_ids[start:start + 900]
        placeholders = ",".join("?" for _ in chunk)
        rows = con.execute(
            f"""select s.*, case when s.last_error is null then s.last_checked_at
                else (select i.checked_at from animego_scan_job_items i
                      where i.anime_id = s.anime_id and i.status = 'completed' and i.error is null
                        and julianday(i.checked_at) is not null
                      order by julianday(i.checked_at) desc limit 1)
                end as last_successful_at
                from animego_title_scan_state s where s.anime_id in ({placeholders})""",
            tuple(chunk),
        ).fetchall()
        states.update({int(row["anime_id"]): dict(row) for row in rows})
    return states


def timestamp_is_due(value, now):
    if not value:
        return True
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed <= now


def personal_candidate_ids(con, user_id, variant_map, candidate_ids):
    profiles = {}

    def profile_for(raw_anime_id):
        animego_id = int(variant_map.get(int(raw_anime_id), int(raw_anime_id)))
        if animego_id not in candidate_ids:
            return None
        return profiles.setdefault(
            animego_id,
            {
                "anime_id": animego_id,
                "watching": False,
                "favorite": False,
                "not_interested": False,
                "last_seen_at": "",
            },
        )

    for row in con.execute(
        """
        select anime_id, is_favorite, watch_status, not_interested, updated_at
        from user_title_state
        where user_id = ?
        """,
        (int(user_id),),
    ).fetchall():
        profile = profile_for(row["anime_id"])
        if profile is None:
            continue
        profile["watching"] |= str(row["watch_status"] or "").casefold() == "watching"
        profile["favorite"] |= bool(row["is_favorite"])
        profile["not_interested"] |= bool(row["not_interested"])
        profile["last_seen_at"] = max(profile["last_seen_at"], str(row["updated_at"] or ""))

    for row in con.execute(
        """
        select anime_id, max(last_seen_at) as last_seen_at
        from user_episode_state
        where user_id = ?
          and (started_at is not null or engaged_seconds > 0)
        group by anime_id
        """,
        (int(user_id),),
    ).fetchall():
        profile = profile_for(row["anime_id"])
        if profile is not None:
            profile["last_seen_at"] = max(
                profile["last_seen_at"], str(row["last_seen_at"] or "")
            )

    eligible = [
        profile
        for profile in profiles.values()
        if profile["watching"] or profile["favorite"]
        if not profile["not_interested"] or profile["watching"] or profile["favorite"]
    ]
    eligible.sort(
        key=lambda profile: (
            not profile["watching"],
            not profile["favorite"],
            profile["last_seen_at"],
            -profile["anime_id"],
        ),
        reverse=False,
    )
    # ISO UTC timestamps sort chronologically. Reverse only the recency portion
    # while retaining watching/favorite as the leading priorities.
    eligible.sort(key=lambda profile: profile["last_seen_at"], reverse=True)
    eligible.sort(key=lambda profile: not profile["favorite"])
    eligible.sort(key=lambda profile: not profile["watching"])
    return [profile["anime_id"] for profile in eligible]


def scan_age_days(value, now, *, default):
    if not value:
        return default
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return max(0.0, (now - parsed).total_seconds() / 86400)
    except ValueError:
        return default


def partial_scan_weight(row, state, now):
    # Successful checks, not attempts, reset priority. Unseen titles start
    # with 30 days of age; cap stale priority at 90 days to retain exploration.
    age = min(90.0, scan_age_days(state.get("last_successful_at"), now, default=30.0))
    changes = [state.get("last_changed_at"), row["last_content_change_at"]]
    change_age = min(scan_age_days(value, now, default=14.0) for value in changes)
    return (1.0 + age) * (1.0 + max(0.0, 1.0 - change_age / 14.0))


def select_scan_items(
    con,
    mode,
    user_id,
    *,
    current_anime_id=None,
    variant_map=None,
    rng=None,
    now=None,
):
    if mode not in SCAN_MODES:
        raise ValueError("mode must be 'partial', 'extended' or 'full'")
    now = now or now_utc()
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    rows = scan_candidate_rows(con, include_completed=True, include_all_sources=True)
    row_by_id = {int(row["id"]): row for row in rows}
    if mode == "full":
        return [(anime_id, "full") for anime_id in row_by_id]
    variant_map = {int(key): int(value) for key, value in (variant_map or {}).items()}
    by_group = {}
    for anime_id in row_by_id:
        by_group.setdefault(variant_map.get(anime_id, anime_id), []).append(anime_id)
    observations = {int(row["anime_id"]): row for row in con.execute("select * from scan_source_observations")}
    incomplete_groups = {
        group for group, ids in by_group.items()
        if not scan_observations.completed_group([row_by_id[anime_id] for anime_id in ids], observations, now)
    }
    states = scan_state_by_anime_id(con, list(row_by_id))
    personal = personal_candidate_ids(con, user_id, variant_map, set(row_by_id))
    personal_groups = set(personal)
    cutoff = now - dt.timedelta(hours=SCAN_RECENCY_HOURS)

    def recently_checked(ids):
        # The title is fresh only when every linked source has a recent
        # successful check. An error must not replace the preceding success.
        checked = [scan_observations.parse_time((states.get(anime_id) or {}).get("last_successful_at"))
                   for anime_id in ids]
        return all(timestamp is not None and cutoff < timestamp <= now for timestamp in checked)

    due_groups = {group for group, ids in by_group.items() if not recently_checked(ids)}
    if mode == "extended":
        return [
            (anime_id, "full")
            for anime_id in row_by_id
            if variant_map.get(anime_id, anime_id) in incomplete_groups
            and (variant_map.get(anime_id, anime_id) in personal_groups
                 or variant_map.get(anime_id, anime_id) in due_groups)
        ]

    selected = {}

    def add(anime_id, reason):
        anime_id = int(anime_id)
        if anime_id not in row_by_id or anime_id in selected:
            return False
        selected[anime_id] = reason
        return True

    if current_anime_id is not None:
        try:
            current_id = int(current_anime_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("current_anime_id must be a positive integer") from exc
        if current_id <= 0:
            raise ValueError("current_anime_id must be a positive integer")
        current_group = variant_map.get(current_id, current_id)
        if current_group in incomplete_groups and current_group in personal_groups:
            for source_id in by_group.get(current_group, [current_id]):
                add(source_id, "current")

    for anime_id in personal:
        if anime_id not in incomplete_groups:
            continue
        for source_id in by_group.get(anime_id, [anime_id]):
            add(source_id, "personal")

    # Exponential races implement weighted sampling without replacement.
    # Sorting IDs first also makes seeded tests reproducible across processes.
    picker = rng or random.SystemRandom()
    ranked = []
    for group, ids in sorted(by_group.items()):
        if (group not in incomplete_groups or group not in due_groups
                or any(anime_id in selected for anime_id in ids)):
            continue
        weight = max(partial_scan_weight(row_by_id[anime_id], states.get(anime_id) or {}, now) for anime_id in ids)
        key = -math.log(max(1e-15, 1.0 - picker.random())) / weight
        ranked.append((key, group))
    for _, group in sorted(ranked)[:PARTIAL_EXTRA_LIMIT]:
        for anime_id in by_group[group]:
            add(anime_id, "random")

    return list(selected.items())


def task_payloads(con, selected):
    tasks = []
    for anime_id, selection_reason in selected:
        row = con.execute(
            "select id, title, url, source, fields_json, source_retired_at from anime where id = ?",
            (int(anime_id),),
        ).fetchone()
        if row is None:
            continue
        tasks.append(
            {
                "anime_id": int(row["id"]),
                "title": row["title"],
                "url": None if row["source_retired_at"] else row["url"],
                "source_retired": bool(row["source_retired_at"]),
                "source": row["source"] if row["source"] in {"animego", "kinogoo", "yummyanime", "kinogo"} else "unsupported",
                "season": int(json.loads(row["fields_json"] or "{}").get("Kinogoo season", 1)),
                "known_episode_ids": [],
                "selection_reason": selection_reason,
            }
        )
    return tasks


def job_payload(row):
    if row is None:
        return None
    payload = {
        key: row[key]
        for key in (
            "id",
            "user_id",
            "mode",
            "status",
            "created_at",
            "expires_at",
            "completed_at",
            "total_items",
            "checked_items",
            "new_episode_count",
            "new_provider_count",
            "error_count",
            "last_error",
        )
    }
    if "requested_mode" in row.keys() and row["requested_mode"]:
        payload["mode"] = "full" if row["requested_mode"] == "full_v2" else row["requested_mode"]
    for key in (
        "id",
        "user_id",
        "total_items",
        "checked_items",
        "new_episode_count",
        "new_provider_count",
        "error_count",
    ):
        payload[key] = int(payload[key] or 0)
    payload["remaining_items"] = max(0, payload["total_items"] - payload["checked_items"])
    return payload


def load_job(con, job_id):
    return con.execute(
        "select j.*, coalesce(m.mode,j.mode) as requested_mode from animego_scan_jobs j "
        "left join animego_scan_job_modes m on m.job_id=j.id where j.id = ?", (int(job_id),)
    ).fetchone()


def check_active_job(db_path, now):
    """Avoid a network refresh when a non-expired scan is already running."""
    con = connect(db_path)
    try:
        row = con.execute(
            "select j.*, coalesce(m.mode,j.mode) as requested_mode from animego_scan_jobs j "
            "left join animego_scan_job_modes m on m.job_id=j.id "
            "where j.status='running' and j.expires_at > ? limit 1",
            (iso_timestamp(now),),
        ).fetchone()
        if row is not None:
            raise ScanConflictError(job_payload(row))
    finally:
        con.close()


def expire_stale_jobs(con, now=None):
    now_value = iso_timestamp(now)
    rows = con.execute(
        """
        select j.*, coalesce(m.mode,j.mode) as requested_mode
        from animego_scan_jobs j left join animego_scan_job_modes m on m.job_id=j.id
        where j.status = 'running' and j.expires_at <= ?
        """,
        (now_value,),
    ).fetchall()
    for row in rows:
        con.execute(
            """
            update animego_scan_jobs
            set status = 'expired', completed_at = ?, last_error = coalesce(last_error, 'scan expired')
            where id = ? and status = 'running'
            """,
            (now_value, row["id"]),
        )
        if row["content_update_run_id"]:
            content_updates.finish_run(
                con,
                row["content_update_run_id"],
                row["created_at"],
                "failed",
                {"animego_user_scan": job_payload(row)},
                error="scan expired",
            )
    return len(rows)


def create_scan_job(
    db_path,
    user,
    mode,
    *,
    current_anime_id=None,
    variant_map=None,
    origin=None,
    rng=None,
    now=None,
    source_snapshots=None,
):
    if mode not in SCAN_MODES:
        raise ValueError("mode must be 'partial', 'extended' or 'full'")
    user_keys = set(user.keys()) if hasattr(user, "keys") else set()
    user_id = int(user["id"])
    user_email = user["email"] if "email" in user_keys else None
    user_name = user["name"] if "name" in user_keys else None
    now = now or now_utc()
    created_at = iso_timestamp(now)
    expires_at = iso_timestamp(now + dt.timedelta(seconds=JOB_TTL_SECONDS))
    token = secrets.token_urlsafe(32)
    con = connect(db_path)
    try:
        ensure_schema(con)
        con.execute("begin immediate")
        expire_stale_jobs(con, now)
        active = con.execute(
            "select j.*, coalesce(m.mode,j.mode) as requested_mode from animego_scan_jobs j "
            "left join animego_scan_job_modes m on m.job_id=j.id where j.status = 'running' limit 1"
        ).fetchone()
        if active is not None:
            con.rollback()
            raise ScanConflictError(job_payload(active))

        if mode == "extended" and source_snapshots is not None:
            title_activity.apply_source_snapshots(con, source_snapshots, now)

        selected = select_scan_items(
            con,
            mode,
            user_id,
            current_anime_id=current_anime_id,
            variant_map=variant_map,
            rng=rng,
            now=now,
        )
        tasks = task_payloads(con, selected)
        run_id = content_updates.create_run(
            con,
            f"user-{mode}",
            "user-animego-scan",
            sorted({"animego"} | ({"yummyanime"} if mode == "full" else set()) | {task["source"] for task in tasks if task["source"]}),
            started_at=created_at,
        )
        job_status = "running" if tasks or mode != "partial" else "completed"
        cursor = con.execute(
            """
            insert into animego_scan_jobs (
                user_id, user_email, user_name, mode, status, token_hash,
                current_anime_id, content_update_run_id, created_at, expires_at,
                completed_at, total_items
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                user_email,
                user_name,
                "full" if mode == "extended" else mode,
                job_status,
                hash_token(token),
                int(current_anime_id) if current_anime_id is not None else None,
                run_id,
                created_at,
                expires_at,
                created_at if job_status == "completed" else None,
                len(tasks),
            ),
        )
        job_id = int(cursor.lastrowid)
        con.execute("insert into scan_observation_jobs(job_id) values (?)", (job_id,))
        if mode in {"extended", "full"}:
            con.execute("insert into animego_scan_job_modes(job_id, mode) values (?, ?)",
                        (job_id, "full_v2" if mode == "full" else "extended"))
        for position, task in enumerate(tasks):
            con.execute(
                """
                insert into animego_scan_job_items (
                    job_id, anime_id, position, selection_reason
                ) values (?, ?, ?, ?)
                """,
                (job_id, task["anime_id"], position, task["selection_reason"]),
            )
        if job_status == "completed":
            completed = load_job(con, job_id)
            content_updates.finish_run(
                con,
                run_id,
                created_at,
                "success",
                {"animego_user_scan": job_payload(completed), "no_work": True},
            )
        con.commit()
        row = load_job(con, job_id)
        return {
            "job": job_payload(row),
            "token": token,
            "tasks": tasks,
            "origin": origin,
            "discovery": mode != "partial",
            "yummy_discovery": mode == "full",
        }
    except sqlite3.IntegrityError as exc:
        con.rollback()
        active = con.execute(
            "select j.*, coalesce(m.mode,j.mode) as requested_mode from animego_scan_jobs j "
            "left join animego_scan_job_modes m on m.job_id=j.id where j.status = 'running' limit 1"
        ).fetchone()
        if active is not None:
            raise ScanConflictError(job_payload(active)) from exc
        raise
    finally:
        con.close()


def discover_scan_titles(db_path, job_id, token, payload):
    """Parse browser-fetched listing HTML; queue candidates without publishing empty cards."""
    if not isinstance(payload, dict) or type(payload.get("page")) is not int:
        raise ValueError("discovery requires a page number")
    page = payload["page"]
    if not 1 <= page <= 500:
        raise ValueError("discovery page limit reached")
    html = validated_text(payload.get("html"), "html", maximum=MAX_RESULT_BODY_BYTES)
    not_found = payload.get("not_found") is True
    con = connect(db_path)
    try:
        ensure_schema(con)
        con.execute("begin immediate")
        job = authenticate_job(con, job_id, token, require_running=True)
        if job["mode"] != "full":
            raise ValueError("discovery requires a full scan")
        previous = con.execute("select done from animego_scan_pages where job_id=? and page=?", (job_id, page)).fetchone()
        if previous is None:
            if page > 1:
                predecessor = con.execute("select done from animego_scan_pages where job_id=? and page=?", (job_id, page - 1)).fetchone()
                if predecessor is None or predecessor["done"]:
                    raise ValueError("discovery pages must be sequential")
            items = animego.parse_listing(html or "")
            empty_listing = (
                page > 1 and not items and isinstance(html, str)
                and bool(re.search(r'class=["\'][^"\']*\bani-list\b', html))
            )
            if not items and not (not_found and page > 1) and not empty_listing:
                # A changed parser or challenge page must not silently mean success.
                raise ValueError("listing contains no recognizable titles")
            fingerprint = payload_sha256(sorted(item["id"] for item in items))
            repeated = con.execute("select 1 from animego_scan_pages where job_id=? and page>0 and fingerprint=?", (job_id, fingerprint)).fetchone()
            done = (not_found and page > 1) or empty_listing or bool(repeated)
            if not done:
                seen = {row[0] for row in con.execute("select anime_id from animego_scan_discovery where job_id=?", (job_id,))}
                for item in items:
                    if not 0 < item["id"] < 10_000_000:
                        raise ValueError("invalid listing title id")
                    if item["id"] in seen or con.execute("select 1 from anime where id=?", (item["id"],)).fetchone():
                        continue
                    con.execute("insert into animego_scan_discovery(job_id, anime_id, page, item_json) values (?, ?, ?, ?)", (job_id, item["id"], page, json.dumps(item)))
                    seen.add(item["id"])
                    con.execute("update animego_scan_jobs set total_items=total_items+1 where id=?", (job_id,))
            con.execute("insert into animego_scan_pages values (?, ?, ?, ?)", (job_id, page, int(done), fingerprint))
        else:
            done = bool(previous["done"])
        tasks = []
        for row in con.execute("select * from animego_scan_discovery where job_id=? and page=? order by anime_id", (job_id, page)):
            item = json.loads(row["item_json"])
            tasks.append({"anime_id": item["id"], "title": item["title"], "url": item["url"], "known_episode_ids": [], "selection_reason": "discovery"})
        con.commit()
        return {"tasks": tasks, "done": done, "job": job_payload(load_job(con, job_id))}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _yummy_discovery_result(con, job_id, page):
    page_row = con.execute("select done from animego_scan_pages where job_id=? and page=?", (job_id, -page)).fetchone()
    if page_row is None:
        return None
    tasks = []
    for row in con.execute("select item_json from animego_scan_discovery where job_id=? and page=? order by anime_id", (job_id, -page)):
        item = json.loads(row["item_json"])
        tasks.append({"anime_id": item["id"], "title": item["title"], "url": item["url"],
                      "source": "yummyanime", "known_episode_ids": [], "selection_reason": "discovery"})
    return {"tasks": tasks, "done": bool(page_row["done"]), "job": job_payload(load_job(con, job_id))}


def discover_yummy_titles(db_path, job_id, token, page, *, now=None):
    """Discover a bounded page of new YummyAni titles, without publishing cards."""
    if type(page) is not int or not 1 <= page <= MAX_YUMMY_DISCOVERY_PAGES:
        raise ValueError("YummyAnime discovery page limit reached")
    con = connect(db_path)
    try:
        job = authenticate_job(con, job_id, token, require_running=True, now=now)
        if job["requested_mode"] != "full_v2":
            raise ValueError("YummyAnime discovery requires a new full scan")
        previous = _yummy_discovery_result(con, job_id, page)
        if previous is not None:
            return previous
        if page > 1:
            predecessor = con.execute("select done from animego_scan_pages where job_id=? and page=?", (job_id, -(page - 1))).fetchone()
            if predecessor is None or predecessor["done"]:
                raise ValueError("YummyAnime discovery pages must be sequential")
    finally:
        con.close()
    if not SOURCE_FETCH_LIMIT.acquire(blocking=False):
        raise ScanOperationBusyError("Другой источник уже проверяется; повторите попытку.")
    try:
        query = urlencode({"limit": YUMMY_DISCOVERY_PAGE_SIZE, "offset": (page - 1) * YUMMY_DISCOVERY_PAGE_SIZE,
                           "sort": "id", "sort_forward": "false"})
        try:
            payload = yummy.fetch_json(f"{yummy.YUMMYANI_API_BASE}/anime?{query}", timeout=12)
        except Exception as exc:
            raise ScanSourceUnavailable("Не удалось получить каталог YummyAnime. Повторите попытку.") from exc
        rows = payload.get("response") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or len(rows) > YUMMY_DISCOVERY_PAGE_SIZE or (page == 1 and not rows):
            raise ScanSourceUnavailable("Каталог YummyAnime вернул неполный или неверный ответ.")
        entries = []
        ids = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ScanSourceUnavailable("Каталог YummyAnime вернул неверную карточку.")
            source_id, slug = row.get("anime_id"), row.get("anime_url")
            if type(source_id) is not int or not 0 < source_id < 10_000_000 or source_id in ids:
                raise ScanSourceUnavailable("Каталог YummyAnime вернул неверный идентификатор.")
            if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,199}", slug):
                raise ScanSourceUnavailable("Каталог YummyAnime вернул неподдерживаемый адрес.")
            ids.add(source_id)
            title = validated_text(row.get("title") or slug, "title", required=True, maximum=1000)
            entries.append({"id": yummy.internal_modern_anime_id(source_id), "source": "yummyanime",
                            "source_id": yummy.modern_source_id(source_id), "slug": slug, "title": title,
                            "url": f"{yummy.YUMMYANI_BASE_URL}/catalog/item/{slug}"})
        done = len(rows) < YUMMY_DISCOVERY_PAGE_SIZE
        fingerprint = payload_sha256(sorted(ids))
        con = connect(db_path)
        try:
            ensure_schema(con)
            con.execute("begin immediate")
            job = authenticate_job(con, job_id, token, require_running=True, now=now)
            previous = _yummy_discovery_result(con, job_id, page)
            if previous is not None:
                con.commit()
                return previous
            if page > 1:
                predecessor = con.execute("select done from animego_scan_pages where job_id=? and page=?", (job_id, -(page - 1))).fetchone()
                if predecessor is None or predecessor["done"]:
                    raise ValueError("YummyAnime discovery pages must be sequential")
            if rows and con.execute("select 1 from animego_scan_pages where job_id=? and page<0 and fingerprint=?", (job_id, fingerprint)).fetchone():
                raise ScanSourceUnavailable("Каталог YummyAnime повторил страницу; поиск не завершён.")
            for item in entries:
                existing = con.execute("select source,source_id from anime where id=?", (item["id"],)).fetchone()
                if existing is not None:
                    if existing["source"] != "yummyanime" or str(existing["source_id"]) != item["source_id"]:
                        raise ValueError("YummyAnime title id belongs to another source")
                    continue
                cursor = con.execute("insert or ignore into animego_scan_discovery(job_id,anime_id,page,item_json) values (?,?,?,?)",
                                     (job_id, item["id"], -page, json.dumps(item, ensure_ascii=False)))
                if cursor.rowcount:
                    con.execute("update animego_scan_jobs set total_items=total_items+1 where id=?", (job_id,))
            con.execute("insert into animego_scan_pages(job_id,page,done,fingerprint) values (?,?,?,?)", (job_id, -page, int(done), fingerprint))
            result = _yummy_discovery_result(con, job_id, page)
            con.commit()
            return result
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
    finally:
        SOURCE_FETCH_LIMIT.release()


def authenticate_job(con, job_id, token, *, require_running=False, now=None):
    if not token:
        raise ScanAuthenticationError("authentication required")
    expire_stale_jobs(con, now)
    row = load_job(con, job_id)
    if row is None or not hmac.compare_digest(row["token_hash"], hash_token(token)):
        raise ScanAuthenticationError("authentication required")
    if require_running and row["status"] == "expired":
        raise ScanExpiredError("scan expired")
    if require_running and row["status"] != "running":
        raise ValueError("scan is not running")
    if require_running and (row["requested_mode"] == "full_v2" or con.execute(
            "select 1 from scan_observation_jobs where job_id=?", (int(job_id),)).fetchone()):
        # Large personal libraries and Extended scans may also take a long time.
        # Renew only within operations whose progress is eventually committed.
        expires_at = iso_timestamp((now or now_utc()) + dt.timedelta(hours=24))
        if expires_at > row["expires_at"]:
            con.execute("update animego_scan_jobs set expires_at=? where id=?", (expires_at, int(job_id)))
            row = load_job(con, job_id)
    return row


def get_scan_job(db_path, job_id, token, *, now=None):
    con = connect(db_path)
    try:
        ensure_schema(con)
        con.execute("begin immediate")
        row = authenticate_job(con, job_id, token, now=now)
        con.commit()
        return {"job": job_payload(row)}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def allowed_player_host(hostname, allowed_hosts):
    hostname = str(hostname or "").rstrip(".").casefold()
    return any(
        hostname == str(allowed).casefold()
        or hostname.endswith("." + str(allowed).casefold())
        for allowed in allowed_hosts
    )


def validated_text(value, field, *, required=False, maximum=20_000):
    if value is None:
        if required:
            raise ValueError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{field} is required")
    if len(value) > maximum:
        raise ValueError(f"{field} is too long")
    return value or None


def validated_provider(provider, allowed_hosts):
    if not isinstance(provider, dict):
        raise ValueError("provider must be an object")
    provider_id = validated_text(
        provider.get("provider_id"), "provider_id", required=True, maximum=300
    )
    provider_title = validated_text(
        provider.get("provider_title"), "provider_title", maximum=500
    )
    translation_id = provider.get("translation_id")
    if type(translation_id) is not int or not 0 <= translation_id < 2_000_000_000:
        raise ValueError("translation_id must be a non-negative integer")
    translation_title = validated_text(
        provider.get("translation_title"),
        "translation_title",
        required=True,
        maximum=500,
    )
    raw_url = validated_text(
        provider.get("embed_url"), "embed_url", required=True, maximum=MAX_PROVIDER_URL
    )
    normalized_url = animego.normalize_embed_url(raw_url)
    try:
        parsed = urlparse("https:" + normalized_url if normalized_url.startswith("//") else normalized_url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or "\\" in parsed.netloc
        or not allowed_player_host(parsed.hostname, allowed_hosts)
    ):
        return None
    # The browser and urllib can serialize the same URL differently. Derive
    # both stored fields from the validated address on the server.
    embed_host = animego.embed_host(normalized_url)
    redacted = animego.redact_embed_url(normalized_url)
    if not embed_host or not redacted:
        return None
    return {
        "provider_id": provider_id,
        "provider_title": provider_title,
        "translation_id": translation_id,
        "translation_title": translation_title,
        "embed_host": embed_host,
        "embed_url": normalized_url,
        "embed_url_redacted": redacted,
    }


def add_scan_error(message, previous):
    return (f"{message} {previous}" if previous else message)[:MAX_ERROR_TEXT]


def scan_result_detail(payload):
    html = validated_text(payload.get("detail_html"), "detail_html", maximum=MAX_RESULT_BODY_BYTES)
    detail = animego.parse_detail(html) if html else None
    return detail if detail and detail["title"] else None


def validate_result_payload(payload, assigned_anime_id, allowed_hosts, *, max_episode_id=2_000_000_000):
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    anime_id = payload.get("anime_id")
    if type(anime_id) is not int or anime_id != int(assigned_anime_id):
        raise ValueError("anime_id is not assigned to this result")
    error = validated_text(payload.get("error"), "error", maximum=MAX_ERROR_TEXT)
    metadata_error = validated_text(payload.get("metadata_error"), "metadata_error", maximum=MAX_ERROR_TEXT)
    episodes = payload.get("episodes", [])
    if not isinstance(episodes, list):
        raise ValueError("episodes must be an array")
    if len(episodes) > MAX_RESULT_EPISODES:
        raise ValueError("too many episodes in result")
    if error and episodes:
        raise ValueError("error result cannot include episodes")

    normalized = []
    episode_ids = set()
    provider_count = 0
    skipped_players = 0
    for snapshot in episodes:
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("episode"), dict):
            raise ValueError("episode snapshot must contain an episode object")
        episode = snapshot["episode"]
        episode_id = episode.get("id")
        if type(episode_id) is not int or not 0 < episode_id < max_episode_id:
            raise ValueError("episode id must be a positive integer")
        if episode_id in episode_ids:
            raise ValueError(f"duplicate episode id: {episode_id}")
        episode_ids.add(episode_id)
        normalized_episode = {"id": episode_id}
        for key in ("number", "title", "release_label", "episode_type", "description"):
            normalized_episode[key] = validated_text(episode.get(key), key)

        providers = snapshot.get("providers")
        if not isinstance(providers, list) or not providers:
            raise ValueError(f"episode {episode_id} requires at least one playable provider")
        if len(providers) > MAX_PROVIDERS_PER_EPISODE:
            raise ValueError(f"episode {episode_id} has too many providers")
        provider_count += len(providers)
        if provider_count > MAX_RESULT_PROVIDERS:
            raise ValueError("too many providers in result")
        normalized_providers = []
        for provider in providers:
            player = validated_provider(provider, allowed_hosts)
            if player is None:
                skipped_players += 1
            else:
                normalized_providers.append(player)
        if not normalized_providers:
            continue
        identities = [
            (provider["provider_id"], provider["translation_id"])
            for provider in normalized_providers
        ]
        if len(identities) != len(set(identities)):
            raise ValueError(f"episode {episode_id} contains duplicate providers")
        normalized.append(
            {
                "episode": normalized_episode,
                "providers": normalized_providers,
            }
        )
    if skipped_players:
        metadata_error = add_scan_error(
            f"Пропущено плееров с недопустимыми адресами: {skipped_players}.", metadata_error
        )
    return {
        "anime_id": anime_id,
        "episodes": normalized,
        "error": error,
        "metadata_error": metadata_error,
    }


def provider_identity(provider):
    return str(provider["provider_id"]), int(provider["translation_id"])


def provider_audit_key(provider):
    provider_id, translation_id = provider_identity(provider)
    return f"{provider_id}|{translation_id}"


def payload_sha256(payload):
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def title_scan_cooldown(row, changed, consecutive_no_change):
    if row["source"] == "kinogoo":
        return dt.timedelta(hours=18)
    if changed:
        return dt.timedelta(hours=6 if title_is_ongoing(row) else 24)
    if title_is_ongoing(row):
        multiplier = min(3, 1 + int(consecutive_no_change or 0) // 3)
        return dt.timedelta(hours=18 * multiplier)
    return dt.timedelta(days=7)


def update_title_scan_state(
    con,
    anime_row,
    user_id,
    job_id,
    *,
    new_episode_count=0,
    new_provider_count=0,
    error=None,
    checked_at=None,
):
    checked_at = checked_at or now_utc()
    existing = con.execute(
        "select * from animego_title_scan_state where anime_id = ?",
        (int(anime_row["id"]),),
    ).fetchone()
    previous_no_change = int(existing["consecutive_no_change"] or 0) if existing else 0
    changed = bool(new_episode_count or new_provider_count)
    no_change = 0 if changed else previous_no_change + (0 if error else 1)
    if error:
        next_eligible = checked_at + dt.timedelta(minutes=30)
    else:
        next_eligible = checked_at + title_scan_cooldown(anime_row, changed, no_change)
    con.execute(
        """
        insert into animego_title_scan_state (
            anime_id, last_checked_at, last_changed_at, next_eligible_at,
            consecutive_no_change, last_error, last_checked_by_user_id, last_scan_job_id
        ) values (?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(anime_id) do update set
            last_checked_at = excluded.last_checked_at,
            last_changed_at = case
                when excluded.last_changed_at is not null then excluded.last_changed_at
                else animego_title_scan_state.last_changed_at
            end,
            next_eligible_at = excluded.next_eligible_at,
            consecutive_no_change = excluded.consecutive_no_change,
            last_error = excluded.last_error,
            last_checked_by_user_id = excluded.last_checked_by_user_id,
            last_scan_job_id = excluded.last_scan_job_id
        """,
        (
            int(anime_row["id"]),
            iso_timestamp(checked_at),
            iso_timestamp(checked_at) if changed else None,
            iso_timestamp(next_eligible),
            no_change,
            error,
            int(user_id),
            int(job_id),
        ),
    )


def insert_content_events(
    con,
    job,
    item,
    anime_row,
    episode,
    new_providers,
    *,
    episode_was_playable,
    existing_translation_keys,
):
    if not new_providers:
        return 0
    metadata = {
        "reason": f"user {anime_row['source']} scan",
        "selection_reason": item["selection_reason"],
        "scan_job_id": int(job["id"]),
        "added_by_user_id": int(job["user_id"]),
    }
    if not episode_was_playable:
        return int(
            content_updates.insert_event(
                con,
                job["content_update_run_id"],
                "new_episode",
                int(anime_row["id"]),
                episode_id=int(episode["id"]),
                source=anime_row["source"],
                source_id=str(anime_row["source_id"] or anime_row["id"]),
                episode_number=episode.get("number"),
                title=anime_row["title"],
                description=f"Добавлена серия {episode.get('number') or ''}".strip(),
                metadata={**metadata, "provider_count": len(new_providers)},
            )
        )

    event_count = 0
    emitted_translations = set()
    for video_source_id, provider in new_providers:
        translation_key = content_updates.normalize_key(provider.get("translation_title"))
        if translation_key not in existing_translation_keys and translation_key not in emitted_translations:
            emitted_translations.add(translation_key)
            event_count += int(
                content_updates.insert_event(
                    con,
                    job["content_update_run_id"],
                    "new_translation",
                    int(anime_row["id"]),
                    episode_id=int(episode["id"]),
                    video_source_id=video_source_id,
                    source=anime_row["source"],
                    source_id=str(anime_row["source_id"] or anime_row["id"]),
                    episode_number=episode.get("number"),
                    translation_title=provider["translation_title"],
                    provider_title=provider.get("provider_title"),
                    title=anime_row["title"],
                    description=f"Новая озвучка: {provider['translation_title']}",
                    metadata=metadata,
                    dedupe_key=content_updates.event_dedupe_key(
                        "new_translation",
                        int(anime_row["id"]),
                        int(episode["id"]),
                        translation_title=provider["translation_title"],
                    ),
                )
            )
        else:
            event_count += int(
                content_updates.insert_event(
                    con,
                    job["content_update_run_id"],
                    "new_provider",
                    int(anime_row["id"]),
                    episode_id=int(episode["id"]),
                    video_source_id=video_source_id,
                    source=anime_row["source"],
                    source_id=str(anime_row["source_id"] or anime_row["id"]),
                    episode_number=episode.get("number"),
                    translation_title=provider["translation_title"],
                    provider_title=provider.get("provider_title"),
                    title=anime_row["title"],
                    description=f"Новый плеер: {provider.get('provider_title') or 'без названия'}",
                    metadata={**metadata, "embed_host": provider["embed_host"]},
                    dedupe_key=content_updates.event_dedupe_key(
                        "new_provider",
                        int(anime_row["id"]),
                        int(episode["id"]),
                        provider=provider,
                    ),
                )
            )
    return event_count


def apply_episode_result(con, job, item, anime_row, snapshot, scraped_at):
    episode = snapshot["episode"]
    providers = snapshot["providers"]
    episode_row = con.execute(
        "select * from episodes where id = ?", (int(episode["id"]),)
    ).fetchone()
    if episode_row is not None and int(episode_row["anime_id"]) != int(anime_row["id"]):
        raise ValueError(f"episode {episode['id']} already belongs to another title")
    episode_was_playable = False
    if episode_row is not None:
        episode_was_playable = bool(
            con.execute(
                "select 1 from video_sources where episode_id = ? and embed_url is not null limit 1",
                (int(episode["id"]),),
            ).fetchone()
        )
    else:
        con.execute(
            """
            insert into episodes (
                id, anime_id, number, title, release_label, episode_type,
                description, has_video, unavailable_reason, scraped_at
            ) values (?, ?, ?, ?, ?, ?, ?, 1, null, ?)
            """,
            (
                int(episode["id"]),
                int(anime_row["id"]),
                episode.get("number"),
                episode.get("title"),
                episode.get("release_label"),
                episode.get("episode_type"),
                episode.get("description"),
                scraped_at,
            ),
        )

    existing_translation_keys = {
        content_updates.normalize_key(row["translation_title"])
        for row in con.execute(
            """
            select translation_title
            from video_sources
            where episode_id = ? and embed_url is not null
            """,
            (int(episode["id"]),),
        ).fetchall()
    }
    new_providers = []
    for provider in providers:
        provider_id, translation_id = provider_identity(provider)
        existing_provider = None
        if job["mode"] == "full":
            # Older AnimeGO data can contain several URLs for one provider.
            # Refresh the matching URL first so updating a sibling cannot
            # collide with its unique (episode, provider, translation, URL) key.
            existing_provider = con.execute(
                """
                select *
                from video_sources
                where episode_id = ?
                  and provider_id = ?
                  and coalesce(translation_id, 0) = ?
                  and embed_url_redacted = ?
                order by case when translation_id = ? then 0 else 1 end, id
                limit 1
                """,
                (int(episode["id"]), provider_id, translation_id,
                 provider["embed_url_redacted"], translation_id),
            ).fetchone()
        if existing_provider is None:
            existing_provider = con.execute(
                """
                select *
                from video_sources
                where episode_id = ?
                  and provider_id = ?
                  and coalesce(translation_id, 0) = ?
                limit 1
                """,
                (int(episode["id"]), provider_id, translation_id),
            ).fetchone()
        con.execute(
            "insert or ignore into translations(id, title) values (?, ?)",
            (translation_id, provider["translation_title"]),
        )
        if existing_provider is not None:
            if existing_provider["embed_url"]:
                if job["mode"] == "full":
                    con.execute(
                        """update video_sources set provider_title = ?, translation_title = ?,
                           embed_host = ?, embed_url = ?, embed_url_redacted = ?, scraped_at = ?
                           where id = ?""",
                        (provider.get("provider_title"), provider["translation_title"],
                         provider["embed_host"], provider["embed_url"],
                         provider["embed_url_redacted"], scraped_at, existing_provider["id"]),
                    )
                continue
            cursor = con.execute(
                """
                update video_sources
                set provider_title = coalesce(provider_title, ?),
                    translation_title = coalesce(translation_title, ?),
                    embed_host = coalesce(embed_host, ?),
                    embed_url = coalesce(embed_url, ?),
                    embed_url_redacted = coalesce(embed_url_redacted, ?),
                    scraped_at = ?
                where id = ? and embed_url is null
                """,
                (
                    provider.get("provider_title"),
                    provider["translation_title"],
                    provider["embed_host"],
                    provider["embed_url"],
                    provider["embed_url_redacted"],
                    scraped_at,
                    int(existing_provider["id"]),
                ),
            )
            if not cursor.rowcount:
                continue
            video_source_id = int(existing_provider["id"])
        else:
            cursor = con.execute(
                """
                insert or ignore into video_sources (
                    anime_id, episode_id, provider_id, provider_title, translation_id,
                    translation_title, embed_host, embed_url, embed_url_redacted, scraped_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(anime_row["id"]),
                    int(episode["id"]),
                    provider_id,
                    provider.get("provider_title"),
                    translation_id,
                    provider["translation_title"],
                    provider["embed_host"],
                    provider["embed_url"],
                    provider["embed_url_redacted"],
                    scraped_at,
                ),
            )
            if not cursor.rowcount:
                continue
            video_source_id = int(cursor.lastrowid)
        con.execute(
            """
            insert or ignore into animego_provider_additions (
                video_source_id, anime_id, source_episode_id, episode_id, provider_key, user_id,
                scan_job_id, added_at, payload_hash
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                video_source_id,
                int(anime_row["id"]),
                int(episode["id"]),
                int(episode["id"]),
                provider_audit_key(provider),
                int(job["user_id"]),
                int(job["id"]),
                scraped_at,
                payload_sha256(provider),
            ),
        )
        new_providers.append((video_source_id, provider))

    became_playable = not episode_was_playable and bool(new_providers)
    if became_playable:
        con.execute(
            """
            update episodes
            set has_video = 1, unavailable_reason = null, scraped_at = ?
            where id = ? and (has_video != 1 or unavailable_reason is not null)
            """,
            (scraped_at, int(episode["id"])),
        )
        con.execute(
            """
            insert or ignore into animego_episode_additions (
                source_episode_id, anime_id, episode_id, user_id, scan_job_id,
                added_at, payload_hash
            ) values (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(episode["id"]),
                int(anime_row["id"]),
                int(episode["id"]),
                int(job["user_id"]),
                int(job["id"]),
                scraped_at,
                payload_sha256(snapshot),
            ),
        )

    insert_content_events(
        con,
        job,
        item,
        anime_row,
        episode,
        new_providers,
        episode_was_playable=episode_was_playable,
        existing_translation_keys=existing_translation_keys,
    )
    return int(became_playable), len(new_providers)


def prepare_source_recovery(db_path, job_id, token, payload, allowed_hosts, resolver, now=None):
    """Fetch an existing canonical alternative outside the database write lock.

    Neither the alternative ID nor its URL is accepted from the client.
    Authentication and assignment checks precede all source network requests.
    """
    if not resolver or not isinstance(payload, dict) or payload.get("upstream_status") != 404 or not payload.get("error"):
        return None
    con = connect(db_path)
    try:
        job = authenticate_job(con, job_id, token, require_running=True, now=now)
        item = con.execute("select * from animego_scan_job_items where job_id=? and anime_id=?", (job_id, payload.get("anime_id"))).fetchone()
        if item is None or item["status"] != "pending":
            return None
        source = con.execute("select source from anime where id=?", (item["anime_id"],)).fetchone()
        if source is None or source["source"] != "animego":
            return None
        validate_result_payload(payload, item["anime_id"], allowed_hosts)
        user_id = job["user_id"]
    finally:
        con.close()
    alternative = resolver(db_path, user_id, payload["anime_id"])
    if not alternative:
        return None
    try:
        if alternative["source"] != "yummyanime" or not yummy.is_modern_yummyani_url(alternative["url"]):
            return None
        if payload.get("browser_recovery") is True:
            if "recovery_response" not in payload and not payload.get("recovery_error"):
                slug = yummy.parse_modern_slug(alternative["url"])
                return {"task": {
                    "anime_id": alternative["id"],
                    "url": f"{yummy.YUMMYANI_API_BASE}/anime/{yummy.quote(slug)}?need_videos=true",
                }}
            if payload.get("recovery_error"):
                return {"error": "YummyAnime: браузер не смог получить данные; проверка не завершена."}
            raw = payload["recovery_response"]
            if not isinstance(raw, dict) or not isinstance(raw.get("response"), dict):
                raise ValueError("invalid alternate API response")
            data = raw["response"]
            if yummy.internal_modern_anime_id(data.get("anime_id")) != int(alternative["id"]):
                raise ValueError("alternate source identity changed")
            counts = data.get("episodes") or {}
            count = int(counts.get("count") or counts.get("aired") or 1)
            videos = data.get("videos") or []
            if not 0 < count <= MAX_RESULT_EPISODES or not isinstance(videos, list) or len(videos) > MAX_RESULT_PROVIDERS:
                raise ValueError("alternate source payload exceeds scan limits")
            fetched, _detail, episodes, providers = yummy.parse_modern_payload(data, alternative["url"])
        else:
            fetched, _detail, episodes, providers = yummy.parse_modern_detail(alternative["url"], timeout=3)
        if int(fetched["id"]) != int(alternative["id"]):
            raise ValueError("alternate source identity changed")
        snapshots = []
        for episode in episodes:
            matches = [p for p in providers if p.get("episode_number") == str(episode.get("number"))]
            if matches:
                if episode["id"] != yummy.internal_episode_id(alternative["id"], episode["number"]):
                    raise ValueError("alternate episode identity mismatch")
                snapshots.append({"episode": episode, "providers": matches})
        if not snapshots:
            raise ValueError("alternate source has no playable episodes")
        normalized = validate_result_payload({"anime_id": alternative["id"], "episodes": snapshots}, alternative["id"], allowed_hosts, max_episode_id=2**53)
        if not normalized["episodes"]:
            return {"error": normalized["metadata_error"]}
        return {"anime_id": alternative["id"], "url": alternative["url"], "result": normalized,
                "source_updated_at": _detail.get("source_updated_at"), "source_detail": _detail}
    except Exception as exc:
        # Keep the original failed result and report the alternative failure.
        # Do not expose upstream URLs containing player tokens or response bodies.
        return {"error": f"YummyAnime recovery failed ({type(exc).__name__})"}


def resolve_assigned_source(db_path, job_id, token, anime_id, allowed_hosts, *, now=None):
    con = connect(db_path)
    try:
        authenticate_job(con, job_id, token, require_running=True, now=now)
        if type(anime_id) is not int or not (con.execute(
            "select 1 from animego_scan_job_items where job_id=? and anime_id=?", (job_id, anime_id)
        ).fetchone() or con.execute(
            "select 1 from animego_scan_discovery where job_id=? and anime_id=? and page<0", (job_id, anime_id)
        ).fetchone()):
            raise ValueError("anime_id is not assigned to this scan")
    finally:
        con.close()
    if not SOURCE_FETCH_LIMIT.acquire(blocking=False):
        raise ScanOperationBusyError("Другой источник уже проверяется; повторите попытку.")
    try:
        return _resolve_assigned_source(db_path, job_id, token, anime_id, allowed_hosts, now=now)
    finally:
        SOURCE_FETCH_LIMIT.release()


def _resolve_assigned_source(db_path, job_id, token, anime_id, allowed_hosts, *, now=None):
    """Fetch an assigned non-AnimeGO source only after authenticating the job."""
    if type(anime_id) is not int or anime_id <= 0:
        raise ValueError("anime_id must be a positive integer")
    con = connect(db_path)
    try:
        authenticate_job(con, job_id, token, require_running=True, now=now)
        item = con.execute(
            "select status from animego_scan_job_items where job_id=? and anime_id=?", (job_id, anime_id)
        ).fetchone()
        candidate = None if item is not None else con.execute(
            "select * from animego_scan_discovery where job_id=? and anime_id=? and page<0", (job_id, anime_id)
        ).fetchone()
        if item is None and candidate is None:
            raise ValueError("anime_id is not assigned to this scan")
        if (item is not None and item["status"] != "pending") or (candidate is not None and candidate["processed"]):
            return submit_scan_result(db_path, job_id, token, {"anime_id": anime_id, "episodes": []}, allowed_hosts, now=now)
        if candidate is not None:
            source = json.loads(candidate["item_json"])
            source["source_retired_at"] = None
        else:
            row = con.execute("select id, source, source_id, url, source_retired_at from anime where id=?", (anime_id,)).fetchone()
            if row is None:
                raise ValueError("assigned title no longer exists")
            source = dict(row)
    finally:
        con.close()

    detail = None
    source_verified = False
    source_complete = False
    payload = {"anime_id": anime_id, "episodes": []}
    try:
        if source["source_retired_at"]:
            raise ValueError("source was retired")
        parsed = urlparse(source["url"] or "")
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or parsed.port is not None or parsed.query
                or (parsed.fragment and source["source"] != "kinogo")):
            raise ValueError("unsupported source address")
        host = (parsed.hostname or "").lower()
        if source["source"] == "yummyanime" and host == "ru.yummyani.me" and parsed.path.startswith("/catalog/item/"):
            slug = yummy.parse_modern_slug(source["url"])
            data, api_url = yummy.fetch_modern_anime(slug, True, timeout=12)
            if not isinstance(data, dict):
                raise ValueError("invalid YummyAni response")
            counts = data.get("episodes") or {}
            count = int(counts.get("count") or counts.get("aired") or 1)
            videos = data.get("videos") or []
            if not 0 < count <= MAX_RESULT_EPISODES or not isinstance(videos, list) or len(videos) > MAX_RESULT_PROVIDERS:
                raise ValueError("YummyAni response exceeds scan limits")
            item, detail, episodes, providers = yummy.parse_modern_payload(data, source["url"], api_url)
        elif source["source"] == "yummyanime" and host == "yummyanime.tv" and re.fullmatch(r"/\d+-[^/]+\.html", parsed.path):
            deadline = time.monotonic() + 30

            def fetch_legacy(url, referer=None, delay=0.0):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("legacy source deadline exceeded")
                check = connect(db_path)
                try:
                    row = load_job(check, job_id)
                    if row is None or row["status"] != "running":
                        raise ScanExpiredError("scan stopped while fetching source")
                finally:
                    check.close()
                return yummy.fetch_text(url, referer=referer, delay=delay, timeout=min(8, remaining))

            html = fetch_legacy(source["url"])
            soup = yummy.BeautifulSoup(html, "lxml")
            fields = yummy.parse_info_fields(soup)
            label = yummy.clean_text(soup.select_one(".inner-page__img .movie-item__label"))
            if not 0 < yummy.parse_episode_count(label, fields) <= MAX_RESULT_EPISODES:
                raise ValueError("YummyAnime response exceeds scan limits")
            if len(yummy.parse_player_params(soup)) > MAX_PROVIDERS_PER_EPISODE:
                raise ValueError("YummyAnime has too many players")
            item, detail, _, _ = yummy.parse_detail(source["url"], html, skip_player=True)
            if int(item["id"]) != anime_id or str(item.get("source_id")) != str(source["source_id"]):
                raise ValueError("source identity changed")
            source_verified = True
            if len(soup.select(".xfplayer[data-params]")) > len(yummy.parse_player_params(soup)):
                payload["metadata_error"] = "Некоторые плееры источника не поддерживаются."
            item, detail, episodes, providers = yummy.parse_detail(source["url"], html, include_embed_urls=True,
                                                                   fetcher=fetch_legacy, strict_players=True)
            if time.monotonic() > deadline:
                raise TimeoutError("legacy source deadline exceeded")
        elif source["source"] == "kinogo" and host in {"kinogo.org", "kinogo.online"}:
            # These older sources are not the Kinogoo player adapter. Try the
            # exact stored page, but never treat an unreadable player as unchanged.
            request = Request(parsed._replace(fragment="").geturl(), headers={"User-Agent": yummy.USER_AGENT})
            with open_validated_url(request, timeout=12, allowed_hosts=(host,)) as response:
                response.read(MAX_RESULT_BODY_BYTES + 1)
            raise ValueError("player format is not supported")
        else:
            raise ValueError("source is not supported")
        if int(item["id"]) != anime_id or str(item.get("source_id")) != str(source["source_id"]):
            raise ValueError("source identity changed")
        source_verified = True
        for episode in episodes:
            matches = [p for p in providers if p.get("episode_number") == str(episode.get("number"))
                       or (p.get("episode_number") is None and len(episodes) == 1)]
            if matches:
                if episode["id"] != yummy.internal_episode_id(anime_id, episode["number"]):
                    raise ValueError("source episode identity changed")
                payload["episodes"].append({"episode": episode, "providers": matches})
        source_complete = bool(episodes) and len(payload["episodes"]) == len(episodes) and not payload.get("metadata_error")
        if source["source"] == "yummyanime" and host == "ru.yummyani.me":
            source_complete = source_complete and len(providers) == len(videos) and not detail.get("unsupported_episode_numbers")
        url_owners = {}
        for snapshot in payload["episodes"]:
            for provider in snapshot["providers"]:
                url_owners.setdefault(provider.get("embed_url"), set()).add(snapshot["episode"]["id"])
        if any(len(ids) > 1 for url, ids in url_owners.items() if url):
            raise ValueError("player URL is ambiguous across episodes")
        if not payload["episodes"] and not (candidate is not None and host == "ru.yummyani.me" and data.get("videos") == []):
            payload["error"] = "Источник не вернул проверяемые плееры; содержимое не подтверждено."
        validate_result_payload(payload, anime_id, allowed_hosts, max_episode_id=2**53)
    except Exception as exc:
        # A page, query, or exception may contain raw player tokens.
        if not source_verified:
            detail = None
        payload["episodes"] = []
        payload["error"] = f"Не удалось проверить источник ({type(exc).__name__})."
    return submit_scan_result(db_path, job_id, token, payload, allowed_hosts, now=now, _source_detail=detail,
                              _source_identity=(source["source"], source["source_id"], source["url"]),
                              _source_complete=source_complete)


def submit_scan_result(db_path, job_id, token, payload, allowed_hosts, *, now=None, fallback_resolver=None,
                       _source_detail=None, _source_identity=None, _source_complete=False):
    recovery = prepare_source_recovery(db_path, job_id, token, payload, allowed_hosts, fallback_resolver, now)
    if recovery and recovery.get("task"):
        return {"recovery_task": recovery["task"]}
    try:
        lock = DatabaseOperationLock(
            db_path,
            wait=True,
            timeout=10,
            operation=f"apply AnimeGO user scan job {job_id}",
        )
        lock.__enter__()
    except OperationLockError as exc:
        raise ScanOperationBusyError(str(exc)) from exc
    try:
        con = connect(db_path)
        try:
            ensure_schema(con)
            con.execute("begin immediate")
            job = authenticate_job(con, job_id, token, require_running=True, now=now)
            anime_id = payload.get("anime_id") if isinstance(payload, dict) else None
            if type(anime_id) is not int:
                raise ValueError("anime_id must be a positive integer")
            item = con.execute(
                "select * from animego_scan_job_items where job_id = ? and anime_id = ?",
                (int(job_id), anime_id),
            ).fetchone()
            normalized = None
            imported_card = False
            if item is None:
                candidate = con.execute("select * from animego_scan_discovery where job_id=? and anime_id=?", (job_id, anime_id)).fetchone()
                if candidate is None:
                    raise ValueError("anime_id is not assigned to this scan")
                if candidate["processed"]:
                    saved = json.loads(candidate["item_json"])
                    con.commit()
                    return {"job": job_payload(job), "result": {"anime_id": anime_id, "status": "already_processed", "item_status": "failed" if saved.get("scan_error") else "completed", "error": saved.get("scan_error"), "new_episode_count": 0, "new_provider_count": 0}}
                discovered_item = json.loads(candidate["item_json"])
                yummy_discovery = candidate["page"] < 0 and discovered_item.get("source") == "yummyanime"
                normalized = validate_result_payload(payload, anime_id, allowed_hosts,
                                                     max_episode_id=2**53 if yummy_discovery else 2_000_000_000)
                expected_source = "yummyanime" if yummy_discovery else "animego"
                existing = None
                detail = None
                candidate_error = normalized["error"]
                if normalized["episodes"]:
                    existing = con.execute("select source from anime where id=?", (anime_id,)).fetchone()
                    if existing and existing["source"] != expected_source:
                        raise ValueError("discovered title id belongs to another source")
                    if existing is None:
                        if yummy_discovery:
                            if _source_detail is None:
                                raise ValueError("YummyAnime discovery requires validated source details")
                            detail = _source_detail
                        else:
                            detail = scan_result_detail(payload)
                        if not detail or not detail["title"]:
                            candidate_error = add_scan_error(UNREADABLE_CARD, normalized["metadata_error"])

                con.execute("update animego_scan_discovery set processed=1 where job_id=? and anime_id=?", (job_id, anime_id))
                if candidate_error or not normalized["episodes"]:
                    candidate_error = candidate_error or normalized["metadata_error"]
                    if candidate_error:
                        discovered_item["scan_error"] = candidate_error
                        con.execute("update animego_scan_discovery set item_json=? where job_id=? and anime_id=?", (json.dumps(discovered_item), job_id, anime_id))
                    con.execute("update animego_scan_jobs set checked_items=checked_items+1, error_count=error_count+?, last_error=coalesce(?, last_error) where id=?", (int(bool(candidate_error)), candidate_error, job_id))
                    con.commit()
                    return {"job": job_payload(load_job(con, job_id)), "result": {"anime_id": anime_id, "status": "failed" if candidate_error else "completed", "error": candidate_error, "new_episode_count": 0, "new_provider_count": 0}}
                if existing is None:
                    animego.upsert_anime(con, discovered_item, detail, iso_timestamp(now), authoritative_metadata=True)
                    imported_card = True
                    content_updates.insert_event(con, job["content_update_run_id"], "new_title", anime_id, source=expected_source,
                                                 source_id=str(discovered_item.get("source_id", anime_id)), title=detail["title"], description="Добавлен тайтл; карточка требует оформления")
                position = con.execute("select coalesce(max(position), -1)+1 from animego_scan_job_items where job_id=?", (job_id,)).fetchone()[0]
                con.execute("insert into animego_scan_job_items(job_id, anime_id, position, selection_reason) values (?, ?, ?, 'discovery')", (job_id, anime_id, position))
                item = con.execute("select * from animego_scan_job_items where job_id=? and anime_id=?", (job_id, anime_id)).fetchone()
            if item["status"] != "pending":
                con.commit()
                return {
                    "job": job_payload(load_job(con, job_id)),
                    "result": {
                        "anime_id": anime_id,
                        "status": "already_processed",
                        "item_status": item["status"],
                        "error": item["error"] if item["status"] == "failed" else None,
                        "recovered_source": "YummyAnime" if str(item["error"] or "").startswith("Recovered via YummyAnime #") else None,
                        "new_episode_count": int(item["new_episode_count"] or 0),
                        "new_provider_count": int(item["new_provider_count"] or 0),
                    },
                }
            anime_row = con.execute(
                "select * from anime where id = ?",
                (anime_id,),
            ).fetchone()
            if anime_row is None:
                raise ValueError("assigned title no longer exists")
            if _source_identity is not None and tuple(anime_row[key] for key in ("source", "source_id", "url")) != _source_identity:
                raise ValueError("assigned source changed while checking")
            if normalized is None:
                normalized = validate_result_payload(payload, anime_id, allowed_hosts,
                                                     max_episode_id=2**53 if anime_row["source"] == "yummyanime" else 2_000_000_000)
            if anime_row["source"] == "kinogoo":
                for snapshot in normalized["episodes"]:
                    if not kinogoo.EPISODE_OFFSET < snapshot["episode"]["id"] < 2_000_000_000:
                        raise ValueError("Invalid Kinogoo episode namespace")
                    for provider in snapshot["providers"]:
                        if provider["embed_host"] != kinogoo.PLAYER_HOST:
                            raise ValueError("Invalid Kinogoo player host")
            checked_at_dt = now or now_utc()
            checked_at = iso_timestamp(checked_at_dt)
            if _source_detail is not None and anime_row["source"] == "yummyanime":
                animego.refresh_release_metadata(con, anime_id, _source_detail, checked_at)
                if _source_detail.get("source_updated_at"):
                    title_activity.record_source_date(con, anime_id, _source_detail["source_updated_at"],
                                                      checked_at_dt, source_kind="yummyanime_video")
            original_source_error = normalized["error"]
            if recovery and recovery.get("error"):
                normalized["error"] = f"{normalized['error']} {recovery['error']}"[:MAX_ERROR_TEXT]
            elif recovery:
                scan_observations.invalidate(con, anime_id)
                alternate_row = con.execute("select * from anime where id=? and source='yummyanime' and url=?", (recovery["anime_id"], recovery["url"])).fetchone()
                if alternate_row is None:
                    raise ValueError("alternate source changed while checking")
                if recovery.get("source_updated_at"):
                    title_activity.record_source_date(con, alternate_row["id"], recovery["source_updated_at"],
                                                      checked_at_dt, source_kind="yummyanime_video")
                if recovery.get("source_detail"):
                    animego.refresh_release_metadata(con, alternate_row["id"], recovery["source_detail"], checked_at)
                update_title_scan_state(con, anime_row, job["user_id"], job_id, error=original_source_error, checked_at=checked_at_dt)
                anime_row = alternate_row
                normalized = recovery["result"]
            if normalized["error"]:
                scan_observations.invalidate(con, anime_id)
                con.execute(
                    """
                    update animego_scan_job_items
                    set status = 'failed', checked_at = ?, error = ?
                    where job_id = ? and anime_id = ?
                    """,
                    (checked_at, normalized["error"], int(job_id), anime_id),
                )
                con.execute(
                    """
                    update animego_scan_jobs
                    set checked_items = checked_items + 1,
                        error_count = error_count + 1,
                        last_error = ?
                    where id = ?
                    """,
                    (normalized["error"], int(job_id)),
                )
                update_title_scan_state(
                    con,
                    anime_row,
                    job["user_id"],
                    job_id,
                    error=normalized["error"],
                    checked_at=checked_at_dt,
                )
                con.commit()
                return {
                    "job": job_payload(load_job(con, job_id)),
                    "result": {
                        "anime_id": anime_id,
                        "status": "failed",
                        "error": normalized["error"],
                        "new_episode_count": 0,
                        "new_provider_count": 0,
                    },
                }

            # Older extension versions omit HTML; preserve compatibility without
            # claiming that their episode-only scans refreshed metadata.
            metadata_error = normalized["metadata_error"]
            if not imported_card and payload.get("detail_html") and anime_row["source"] == "animego":
                detail = scan_result_detail(payload)
                if detail is None:
                    metadata_error = add_scan_error(UNREADABLE_CARD, metadata_error)
                else:
                    animego.upsert_anime(con, dict(anime_row), detail, checked_at, authoritative_metadata=False)

            new_episodes = 0
            new_providers = 0
            for snapshot in normalized["episodes"]:
                episode_count, provider_count = apply_episode_result(
                    con, job, item, anime_row, snapshot, checked_at
                )
                new_episodes += episode_count
                new_providers += provider_count
            con.execute(
                """
                update animego_scan_job_items
                set status = ?, checked_at = ?,
                    new_episode_count = ?, new_provider_count = ?, error = ?
                where job_id = ? and anime_id = ?
                """,
                ("failed" if metadata_error else "completed", checked_at, new_episodes, new_providers,
                 metadata_error or (f"Recovered via YummyAnime #{anime_row['id']}; {original_source_error}" if recovery else None),
                 int(job_id), anime_id),
            )
            con.execute(
                """
                update animego_scan_jobs
                set checked_items = checked_items + 1,
                    new_episode_count = new_episode_count + ?,
                    new_provider_count = new_provider_count + ?,
                    error_count = error_count + ?, last_error = coalesce(?, last_error)
                where id = ?
                """,
                (new_episodes, new_providers, int(bool(metadata_error)), metadata_error, int(job_id)),
            )
            update_title_scan_state(
                con,
                anime_row,
                job["user_id"],
                job_id,
                new_episode_count=new_episodes,
                new_provider_count=new_providers,
                error=metadata_error,
                checked_at=checked_at_dt,
            )
            if con.execute("select 1 from scan_observation_jobs where job_id=?", (job_id,)).fetchone():
                complete = (not metadata_error and not recovery and (
                    (_source_identity is not None and _source_complete)
                    or (_source_identity is None and anime_row["source"] in ("animego", "kinogoo")
                        and payload.get("full_content_checked") is True
                        and (anime_row["source"] != "animego" or bool(payload.get("detail_html"))))
                ))
                if complete:
                    known = {int(row[0]) for row in con.execute("""select distinct e.id from episodes e
                        join video_sources v on v.episode_id=e.id
                        where e.anime_id=? and v.embed_url is not null and trim(v.embed_url)!=''""", (anime_id,))}
                    returned = {snapshot["episode"]["id"] for snapshot in normalized["episodes"]}
                    complete = known <= returned
                scan_observations.record(con, anime_row, normalized["episodes"], checked_at_dt, complete=complete,
                                         started_at=job["created_at"], job_id=job_id, user_id=job["user_id"])
            con.commit()
            return {
                "job": job_payload(load_job(con, job_id)),
                "result": {
                    "anime_id": anime_id,
                    "status": "failed" if metadata_error else "completed",
                    "error": metadata_error,
                    "recovered_source": "YummyAnime" if recovery else None,
                    "new_episode_count": new_episodes,
                    "new_provider_count": new_providers,
                },
            }
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
    finally:
        lock.__exit__(None, None, None)


def validated_completion_payload(payload):
    if payload is None:
        return [], False
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    errors = payload.get("errors", [])
    if not isinstance(errors, list) or len(errors) > MAX_RESULT_EPISODES:
        raise ValueError("errors must be a bounded array")
    normalized = []
    seen = set()
    for error in errors:
        if not isinstance(error, dict):
            raise ValueError("completion error must be an object")
        anime_id = error.get("anime_id")
        if type(anime_id) is not int or anime_id <= 0 or anime_id in seen:
            raise ValueError("completion error has an invalid anime_id")
        seen.add(anime_id)
        normalized.append(
            {
                "anime_id": anime_id,
                "message": validated_text(
                    error.get("message"), "message", required=True, maximum=MAX_ERROR_TEXT
                ),
            }
        )
    stopped = payload.get("stopped", False)
    if type(stopped) is not bool:
        raise ValueError("stopped must be a boolean")
    return normalized, stopped


def complete_scan_job(db_path, job_id, token, payload=None, *, now=None):
    errors, stopped = validated_completion_payload(payload)
    con = connect(db_path)
    try:
        ensure_schema(con)
        con.execute("begin immediate")
        job = authenticate_job(con, job_id, token, now=now)
        if job["status"] == "expired":
            raise ScanExpiredError("scan expired")
        if job["status"] in {"completed", "stopped"}:
            con.commit()
            return {"job": job_payload(job)}
        if job["status"] != "running":
            raise ValueError("scan is not running")
        if not stopped and con.execute(
            "select 1 from animego_scan_job_items where job_id = ? and status = 'pending' limit 1",
            (int(job_id),),
        ).fetchone():
            raise ValueError("scan still has pending titles; submit results or stop the scan")
        if not stopped and job["mode"] == "full":
            if not con.execute("select 1 from animego_scan_pages where job_id=? and page>0 and done=1", (job_id,)).fetchone():
                raise ValueError("full scan discovery is not complete; update the scanner extension")
            if job["requested_mode"] == "full_v2" and not con.execute(
                "select 1 from animego_scan_pages where job_id=? and page<0 and done=1", (job_id,)
            ).fetchone():
                raise ValueError("YummyAnime discovery is not complete")
            if con.execute("select 1 from animego_scan_discovery where job_id=? and processed=0", (job_id,)).fetchone():
                raise ValueError("scan still has pending discovered titles")
        new_errors = 0
        last_error = None
        for error in errors:
            item = con.execute(
                "select * from animego_scan_job_items where job_id = ? and anime_id = ?",
                (int(job_id), error["anime_id"]),
            ).fetchone()
            if item is None:
                # Discovered titles may have no catalog row or job item. A
                # processed result already affected the counters; a stop can
                # also report an error for an assigned but unprocessed title.
                discovered = con.execute(
                    "select processed from animego_scan_discovery "
                    "where job_id = ? and anime_id = ?",
                    (int(job_id), error["anime_id"]),
                ).fetchone()
                if discovered is None:
                    raise ValueError("completion error refers to an unassigned anime_id")
                if not discovered["processed"]:
                    if not stopped:
                        raise ValueError("scan still has pending discovered titles")
                    con.execute(
                        "update animego_scan_discovery set processed = 1 where job_id = ? and anime_id = ?",
                        (int(job_id), error["anime_id"]),
                    )
                    new_errors += 1
                    last_error = error["message"]
                continue
            if item["status"] != "pending":
                continue
            # This is an extension summary, not a posted title result. Preserve
            # checked_items/checked_at so audit consumers can distinguish it.
            con.execute(
                """
                update animego_scan_job_items
                set status = 'failed', error = ?
                where job_id = ? and anime_id = ? and status = 'pending'
                """,
                (error["message"], int(job_id), error["anime_id"]),
            )
            new_errors += 1
            last_error = error["message"]
        completed_at = iso_timestamp(now)
        con.execute(
            """
            update animego_scan_jobs
            set status = ?, completed_at = ?,
                error_count = error_count + ?,
                last_error = coalesce(?, last_error)
            where id = ? and status = 'running'
            """,
            ("stopped" if stopped else "completed", completed_at, new_errors, last_error, int(job_id)),
        )
        completed = load_job(con, job_id)
        content_updates.finish_run(
            con,
            job["content_update_run_id"],
            job["created_at"],
            "partial" if stopped or int(completed["error_count"] or 0) else "success",
            {"animego_user_scan": job_payload(completed)},
            error=completed["last_error"],
        )
        con.commit()
        return {"job": job_payload(completed)}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def build_extension_zip(extension_root):
    root = Path(extension_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"scanner extension directory not found: {root}")
    files = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and not any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(root).parts)
    ]
    if not files:
        raise FileNotFoundError(f"scanner extension directory is empty: {root}")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
            relative = path.relative_to(root).as_posix()
            info = zipfile.ZipInfo(f"animego-scanner/{relative}")
            info.date_time = (1980, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    return output.getvalue()
