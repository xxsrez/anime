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
