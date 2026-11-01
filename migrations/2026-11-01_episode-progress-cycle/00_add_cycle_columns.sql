-- runtime-schema-contract: episode-progress-cycle-v1
alter table user_episode_state add column cycle_event_id integer;
alter table user_episode_state add column cycle_engaged_base integer;
alter table user_episode_state add column cycle_completed_at text;

create table if not exists user_episode_cycle_history (
    id integer primary key autoincrement,
    user_id integer not null references users(id) on delete cascade,
    anime_id integer not null references anime(id) on delete cascade,
    episode_id integer not null references episodes(id) on delete cascade,
    cycle_event_id integer not null,
    completed_at text,
    engaged_seconds integer not null
);
create index if not exists idx_user_episode_cycle_history_title
    on user_episode_cycle_history(user_id, anime_id);
