#!/usr/bin/env python3
"""User-powered, additive-only AnimeGO scan jobs."""

import datetime as dt
import hashlib
import hmac
import io
import json
import random
import re
import secrets
import sqlite3
from pathlib import Path
from urllib.parse import urlparse
import zipfile

import content_updates
import scrape_animego as animego
from scripts.operation_lock import DatabaseOperationLock, OperationLockError


SCAN_MODES = {"partial", "full"}
ACTIVE_JOB_STATUS = "running"
JOB_TTL_SECONDS = 2 * 60 * 60
PARTIAL_SCAN_LIMIT = 15
PARTIAL_PERSONAL_LIMIT = 6
PARTIAL_STALE_LIMIT = 5
PARTIAL_RANDOM_LIMIT = 3
MAX_RESULT_BODY_BYTES = 2 * 1024 * 1024
MAX_COMPLETE_BODY_BYTES = 256 * 1024
MAX_RESULT_EPISODES = 500
MAX_PROVIDERS_PER_EPISODE = 100
MAX_RESULT_PROVIDERS = 5_000
MAX_ERROR_TEXT = 2_000
MAX_PROVIDER_URL = 8_192
ONGOING_STATUSES = {"ongoing", "онгоинг"}


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


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def iso_timestamp(value=None):
    return (value or now_utc()).isoformat(timespec="seconds")


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


def scan_candidate_rows(con):
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
            coalesce(ec.episode_count, 0) as episode_count,
            coalesce(aec.available_episode_count, 0) as available_episode_count
        from anime a
        left join episode_counts ec on ec.anime_id = a.id
        left join available_episode_counts aec on aec.anime_id = a.id
        where a.source = 'animego'
        order by cast(coalesce(a.year, '0') as integer) desc, a.id desc
        """
    ).fetchall()
    return [row for row in rows if title_is_ongoing(row) or title_needs_player_backfill(row)]


def scan_state_by_anime_id(con, anime_ids):
    if not anime_ids:
        return {}
    placeholders = ",".join("?" for _ in anime_ids)
    rows = con.execute(
        f"select * from animego_title_scan_state where anime_id in ({placeholders})",
        tuple(anime_ids),
    ).fetchall()
    return {int(row["anime_id"]): dict(row) for row in rows}


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
        if profile["watching"] or profile["favorite"] or profile["last_seen_at"]
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
        raise ValueError("mode must be 'partial' or 'full'")
    now = now or now_utc()
    rows = scan_candidate_rows(con)
    row_by_id = {int(row["id"]): row for row in rows}
    if mode == "full":
        return [(anime_id, "full") for anime_id in row_by_id]

    states = scan_state_by_anime_id(con, list(row_by_id))
    due_ids = {
        anime_id
        for anime_id in row_by_id
        if timestamp_is_due((states.get(anime_id) or {}).get("next_eligible_at"), now)
    }
    variant_map = {int(key): int(value) for key, value in (variant_map or {}).items()}
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
        add(variant_map.get(current_id, current_id), "current")

    personal_added = 0
    for anime_id in personal_candidate_ids(con, user_id, variant_map, set(row_by_id)):
        if anime_id not in due_ids:
            continue
        if add(anime_id, "personal"):
            personal_added += 1
        if personal_added >= PARTIAL_PERSONAL_LIMIT:
            break

    def stale_key(anime_id):
        state = states.get(anime_id) or {}
        return (str(state.get("last_checked_at") or ""), anime_id)

    remaining_due = [
        anime_id for anime_id in sorted(due_ids, key=stale_key) if anime_id not in selected
    ]
    for anime_id in remaining_due[:PARTIAL_STALE_LIMIT]:
        add(anime_id, "stale")

    random_pool = [anime_id for anime_id in remaining_due if anime_id not in selected]
    random_count = min(PARTIAL_RANDOM_LIMIT, len(random_pool))
    if random_count:
        picker = rng or random.SystemRandom()
        for anime_id in picker.sample(random_pool, random_count):
            add(anime_id, "random")

    for anime_id in remaining_due:
        if len(selected) >= PARTIAL_SCAN_LIMIT:
            break
        add(anime_id, "stale")

    return list(selected.items())[:PARTIAL_SCAN_LIMIT]


def known_playable_episode_ids(con, anime_id):
    return [
        int(row["id"])
        for row in con.execute(
            """
            select e.id
            from episodes e
            where e.anime_id = ?
              and exists (
                  select 1
                  from video_sources vs
                  where vs.episode_id = e.id
                    and vs.embed_url is not null
              )
            order by e.id
            """,
            (int(anime_id),),
        ).fetchall()
    ]


def task_payloads(con, selected):
    tasks = []
    for anime_id, selection_reason in selected:
        row = con.execute(
            "select id, title from anime where id = ? and source = 'animego'",
            (int(anime_id),),
        ).fetchone()
        if row is None:
            continue
        tasks.append(
            {
                "anime_id": int(row["id"]),
                "title": row["title"],
                "known_episode_ids": known_playable_episode_ids(con, row["id"]),
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
    return con.execute("select * from animego_scan_jobs where id = ?", (int(job_id),)).fetchone()


def expire_stale_jobs(con, now=None):
    now_value = iso_timestamp(now)
    rows = con.execute(
        """
        select *
        from animego_scan_jobs
        where status = 'running' and expires_at <= ?
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
):
    if mode not in SCAN_MODES:
        raise ValueError("mode must be 'partial' or 'full'")
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
            "select * from animego_scan_jobs where status = 'running' limit 1"
        ).fetchone()
        if active is not None:
            con.rollback()
            raise ScanConflictError(job_payload(active))

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
            ["animego"],
            started_at=created_at,
        )
        job_status = "running" if tasks else "completed"
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
                mode,
                job_status,
                hash_token(token),
                int(current_anime_id) if current_anime_id is not None else None,
                run_id,
                created_at,
                expires_at,
                created_at if not tasks else None,
                len(tasks),
            ),
        )
        job_id = int(cursor.lastrowid)
        for position, task in enumerate(tasks):
            con.execute(
                """
                insert into animego_scan_job_items (
                    job_id, anime_id, position, selection_reason
                ) values (?, ?, ?, ?)
                """,
                (job_id, task["anime_id"], position, task["selection_reason"]),
            )
        if not tasks:
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
        }
    except sqlite3.IntegrityError as exc:
        con.rollback()
        active = con.execute(
            "select * from animego_scan_jobs where status = 'running' limit 1"
        ).fetchone()
        if active is not None:
            raise ScanConflictError(job_payload(active)) from exc
        raise
    finally:
        con.close()


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
    parsed = urlparse("https:" + normalized_url if normalized_url.startswith("//") else normalized_url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("player URL has an invalid port") from exc
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or not allowed_player_host(parsed.hostname, allowed_hosts)
    ):
        raise ValueError("player URL is not an allowed HTTPS URL")
    embed_host = animego.embed_host(normalized_url)
    redacted = animego.redact_embed_url(normalized_url)
    supplied_host = validated_text(provider.get("embed_host"), "embed_host", maximum=500)
    supplied_redacted = animego.normalize_embed_url(
        validated_text(
            provider.get("embed_url_redacted"),
            "embed_url_redacted",
            required=True,
            maximum=MAX_PROVIDER_URL,
        )
    )
    if not embed_host or not redacted:
        raise ValueError("player URL could not be normalized safely")
    if supplied_host and supplied_host.casefold() != embed_host.casefold():
        raise ValueError("embed_host does not match player URL")
    canonical_supplied = "https:" + supplied_redacted if supplied_redacted.startswith("//") else supplied_redacted
    canonical_redacted = "https:" + redacted if redacted.startswith("//") else redacted
    if canonical_supplied != canonical_redacted:
        raise ValueError("embed_url_redacted does not match player URL")
    return {
        "provider_id": provider_id,
        "provider_title": provider_title,
        "translation_id": translation_id,
        "translation_title": translation_title,
        "embed_host": embed_host,
        "embed_url": normalized_url,
        "embed_url_redacted": redacted,
    }


def validate_result_payload(payload, assigned_anime_id, allowed_hosts):
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    anime_id = payload.get("anime_id")
    if type(anime_id) is not int or anime_id != int(assigned_anime_id):
        raise ValueError("anime_id is not assigned to this result")
    error = validated_text(payload.get("error"), "error", maximum=MAX_ERROR_TEXT)
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
    for snapshot in episodes:
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("episode"), dict):
            raise ValueError("episode snapshot must contain an episode object")
        episode = snapshot["episode"]
        episode_id = episode.get("id")
        if type(episode_id) is not int or not 0 < episode_id < 2_000_000_000:
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
        normalized_providers = [validated_provider(provider, allowed_hosts) for provider in providers]
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
    return {
        "anime_id": anime_id,
        "episodes": normalized,
        "error": error,
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
        "reason": "user AnimeGO scan",
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
                source="animego",
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
                    source="animego",
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
                    source="animego",
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


def submit_scan_result(db_path, job_id, token, payload, allowed_hosts, *, now=None):
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
            if item is None:
                raise ValueError("anime_id is not assigned to this scan")
            if item["status"] != "pending":
                con.commit()
                return {
                    "job": job_payload(load_job(con, job_id)),
                    "result": {
                        "anime_id": anime_id,
                        "status": "already_processed",
                        "new_episode_count": int(item["new_episode_count"] or 0),
                        "new_provider_count": int(item["new_provider_count"] or 0),
                    },
                }
            normalized = validate_result_payload(payload, anime_id, allowed_hosts)
            anime_row = con.execute(
                "select * from anime where id = ? and source = 'animego'",
                (anime_id,),
            ).fetchone()
            if anime_row is None:
                raise ValueError("assigned AnimeGO title no longer exists")
            checked_at_dt = now or now_utc()
            checked_at = iso_timestamp(checked_at_dt)
            if normalized["error"]:
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
                        "new_episode_count": 0,
                        "new_provider_count": 0,
                    },
                }

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
                set status = 'completed', checked_at = ?,
                    new_episode_count = ?, new_provider_count = ?, error = null
                where job_id = ? and anime_id = ?
                """,
                (checked_at, new_episodes, new_providers, int(job_id), anime_id),
            )
            con.execute(
                """
                update animego_scan_jobs
                set checked_items = checked_items + 1,
                    new_episode_count = new_episode_count + ?,
                    new_provider_count = new_provider_count + ?
                where id = ?
                """,
                (new_episodes, new_providers, int(job_id)),
            )
            update_title_scan_state(
                con,
                anime_row,
                job["user_id"],
                job_id,
                new_episode_count=new_episodes,
                new_provider_count=new_providers,
                checked_at=checked_at_dt,
            )
            con.commit()
            return {
                "job": job_payload(load_job(con, job_id)),
                "result": {
                    "anime_id": anime_id,
                    "status": "completed",
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
        new_errors = 0
        last_error = None
        for error in errors:
            item = con.execute(
                "select * from animego_scan_job_items where job_id = ? and anime_id = ?",
                (int(job_id), error["anime_id"]),
            ).fetchone()
            if item is None:
                raise ValueError("completion error refers to an unassigned anime_id")
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
