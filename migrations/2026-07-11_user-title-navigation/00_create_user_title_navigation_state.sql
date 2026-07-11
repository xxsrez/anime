create table if not exists user_title_navigation_state (
    user_id integer not null references users(id) on delete cascade,
    anime_id integer not null references anime(id) on delete cascade,
    episode_id integer references episodes(id) on delete set null,
    episode_number text,
    updated_at text not null,
    primary key (user_id, anime_id)
);

create index if not exists idx_user_title_navigation_user_updated
    on user_title_navigation_state(user_id, updated_at desc);
