create table if not exists user_watch_events (
    id integer primary key autoincrement,
    user_id integer not null references users(id) on delete cascade,
    anime_id integer not null references anime(id) on delete cascade,
    episode_id integer references episodes(id) on delete set null,
    video_source_id integer references video_sources(id) on delete set null,
    client_session_id text not null,
    event_type text not null,
    event_at text not null,
    episode_number text,
    progress_episode_number integer,
    source text,
    source_anime_id integer references anime(id) on delete set null,
    translation_id text,
    translation_title text,
    provider_id text,
    provider_title text,
    embed_host text,
    engaged_seconds integer not null default 0,
    page_visible integer,
    player_focused integer,
    confidence real not null default 0,
    metadata_json text not null default '{}',
    created_at text not null,
    check (event_type in (
        'player_loaded',
        'player_engaged',
        'heartbeat',
        'fullscreen_enter',
        'pip_open',
        'episode_selected',
        'source_changed',
        'page_hidden',
        'session_end'
    ))
);

create table if not exists user_episode_state (
    user_id integer not null references users(id) on delete cascade,
    anime_id integer not null references anime(id) on delete cascade,
    episode_id integer not null references episodes(id) on delete cascade,
    episode_number text,
    progress_episode_number integer,
    video_source_id integer references video_sources(id) on delete set null,
    source text,
    source_anime_id integer references anime(id) on delete set null,
    translation_id text,
    translation_title text,
    provider_id text,
    provider_title text,
    embed_host text,
    first_seen_at text not null,
    last_seen_at text not null,
    started_at text,
    completed_at text,
    engaged_seconds integer not null default 0,
    heartbeat_count integer not null default 0,
    last_event_type text not null,
    last_confidence real not null default 0,
    completion_confidence real,
    updated_at text not null,
    primary key (user_id, anime_id, episode_id)
);

create index if not exists idx_user_watch_events_user_at
    on user_watch_events(user_id, event_at desc);
create index if not exists idx_user_watch_events_session
    on user_watch_events(user_id, client_session_id, event_at);
create index if not exists idx_user_watch_events_episode
    on user_watch_events(user_id, anime_id, episode_id, event_at desc);
create index if not exists idx_user_episode_state_user_seen
    on user_episode_state(user_id, last_seen_at desc);
create index if not exists idx_user_episode_state_anime_progress
    on user_episode_state(user_id, anime_id, progress_episode_number);
