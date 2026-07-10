#!/usr/bin/env python3
import datetime as dt
import json
import re


RECENT_UPDATE_DAYS = 3
EVENT_TYPES = {"new_title", "new_episode", "new_translation", "new_provider"}


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def ensure_schema(con):
    con.executescript(
        """
        create table if not exists content_update_runs (
            id integer primary key autoincrement,
            mode text not null,
            trigger text not null,
            sources_json text not null default '[]',
            started_at text not null,
            finished_at text,
            duration_ms integer,
            status text not null,
            stats_json text not null default '{}',
            error text
        );

        create table if not exists content_update_events (
            id integer primary key autoincrement,
            run_id integer references content_update_runs(id) on delete set null,
            event_type text not null,
            anime_id integer not null references anime(id) on delete cascade,
            episode_id integer references episodes(id) on delete cascade,
            video_source_id integer references video_sources(id) on delete set null,
            source text,
            source_id text,
            episode_number text,
            translation_title text,
            provider_title text,
            title text,
            description text,
            occurred_at text not null,
            dedupe_key text not null unique,
            metadata_json text not null default '{}',
            check (event_type in ('new_title', 'new_episode', 'new_translation', 'new_provider'))
        );

        create index if not exists idx_content_update_events_anime_at
            on content_update_events(anime_id, occurred_at desc);
        create index if not exists idx_content_update_events_occurred_at
            on content_update_events(occurred_at desc);
        create index if not exists idx_content_update_events_type_at
            on content_update_events(event_type, occurred_at desc, id desc);
        create index if not exists idx_content_update_events_run_id
            on content_update_events(run_id);
        create index if not exists idx_content_update_runs_started_at
            on content_update_runs(started_at desc);
        """
    )


def normalize_key(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def provider_key(provider):
    return "|".join(
        normalize_key(provider.get(key))
        for key in (
            "provider_id",
            "provider_title",
            "translation_id",
            "translation_title",
            "embed_url_redacted",
        )
    )


def event_dedupe_key(event_type, anime_id, episode_id=None, provider=None, translation_title=None):
    if event_type == "new_title":
        return f"new_title:{anime_id}"
    if event_type == "new_episode":
        return f"new_episode:{anime_id}:{episode_id}"
    if event_type == "new_translation":
        return f"new_translation:{anime_id}:{episode_id}:{normalize_key(translation_title)}"
    if event_type == "new_provider":
        return f"new_provider:{anime_id}:{episode_id}:{provider_key(provider or {})}"
    raise ValueError(f"unsupported content update event: {event_type}")


def create_run(con, mode, trigger, sources, started_at=None):
    ensure_schema(con)
    started_at = started_at or now_iso()
    cur = con.execute(
        """
        insert into content_update_runs(mode, trigger, sources_json, started_at, status)
        values (?, ?, ?, ?, 'running')
        """,
        (mode, trigger, json.dumps(list(sources or []), ensure_ascii=False), started_at),
    )
    return cur.lastrowid


def finish_run(con, run_id, started_at, status, stats=None, error=None):
    if not run_id:
        return
    finished_at = now_iso()
    duration_ms = None
    try:
        started = dt.datetime.fromisoformat(started_at)
        finished = dt.datetime.fromisoformat(finished_at)
        duration_ms = max(0, int((finished - started).total_seconds() * 1000))
    except ValueError:
        pass
    con.execute(
        """
        update content_update_runs
        set finished_at = ?,
            duration_ms = ?,
            status = ?,
            stats_json = ?,
            error = ?
        where id = ?
        """,
        (
            finished_at,
            duration_ms,
            status,
            json.dumps(stats or {}, ensure_ascii=False, sort_keys=True),
            str(error)[:2000] if error else None,
            run_id,
        ),
    )


def insert_event(
    con,
    run_id,
    event_type,
    anime_id,
    *,
    episode_id=None,
    video_source_id=None,
    source=None,
    source_id=None,
    episode_number=None,
    translation_title=None,
    provider_title=None,
    title=None,
    description=None,
    metadata=None,
    occurred_at=None,
    dedupe_key=None,
):
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unsupported content update event: {event_type}")
    before = con.total_changes
    con.execute(
        """
        insert or ignore into content_update_events (
            run_id,
            event_type,
            anime_id,
            episode_id,
            video_source_id,
            source,
            source_id,
            episode_number,
            translation_title,
            provider_title,
            title,
            description,
            occurred_at,
            dedupe_key,
            metadata_json
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            event_type,
            anime_id,
            episode_id,
            video_source_id,
            source,
            source_id,
            episode_number,
            translation_title,
            provider_title,
            title,
            description,
            occurred_at or now_iso(),
            dedupe_key or event_dedupe_key(event_type, anime_id, episode_id),
            json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
        ),
    )
    return con.total_changes > before


def recent_cutoff(days=RECENT_UPDATE_DAYS):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat(timespec="seconds")
