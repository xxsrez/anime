create table if not exists scan_observation_jobs (
    job_id integer primary key references animego_scan_jobs(id) on delete cascade
);

create table if not exists scan_source_observations (
    anime_id integer primary key references anime(id) on delete cascade,
    source_identity text not null,
    content_fingerprint text not null,
    unchanged_since text not null,
    last_complete_at text not null,
    last_evidence_from text not null,
    last_event_id integer not null default 0,
    last_job_id integer references animego_scan_jobs(id) on delete set null,
    last_user_id integer references users(id) on delete set null,
    last_attempt_complete integer not null check (last_attempt_complete in (0, 1))
);
