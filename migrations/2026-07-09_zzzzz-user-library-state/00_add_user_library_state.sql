-- runtime-schema-contract: user-library-state-v1
alter table user_title_state add column watch_status text;
alter table user_title_state add column not_interested integer not null default 0;
alter table user_title_state add column favorite_updated_at text;
alter table user_title_state add column watch_status_updated_at text;
alter table user_title_state add column not_interested_updated_at text;

update user_title_state
set watch_status = case
        when watched = 1 then 'completed'
        when progress_episode_number is not null then 'watching'
        else null
    end,
    favorite_updated_at = case when is_favorite = 1 then updated_at else null end,
    watch_status_updated_at = case
        when watched = 1 or progress_episode_number is not null then updated_at
        else null
    end;

create index if not exists idx_user_title_state_user_watch_status
    on user_title_state(user_id, watch_status, watch_status_updated_at desc);

create index if not exists idx_user_title_state_user_not_interested
    on user_title_state(user_id, not_interested)
    where not_interested = 1;
