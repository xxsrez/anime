create table if not exists user_manual_episode_completions (
    user_id integer not null references users(id) on delete cascade,
    operation_id text not null,
    anime_id integer not null references anime(id) on delete cascade,
    episode_id integer not null references episodes(id) on delete cascade,
    member_ids text not null,
    before_state text not null,
    after_state text not null,
    created_at text not null,
    undone_at text,
    primary key (user_id, operation_id)
);
