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
create index if not exists idx_content_update_events_run_id
    on content_update_events(run_id);
create index if not exists idx_content_update_runs_started_at
    on content_update_runs(started_at desc);
