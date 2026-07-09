delete from user_watch_events
where not exists (select 1 from users where users.id = user_watch_events.user_id)
   or not exists (select 1 from anime where anime.id = user_watch_events.anime_id);

update user_watch_events
set episode_id = null
where episode_id is not null
  and not exists (select 1 from episodes where episodes.id = user_watch_events.episode_id);

update user_watch_events
set video_source_id = null
where video_source_id is not null
  and not exists (select 1 from video_sources where video_sources.id = user_watch_events.video_source_id);

update user_watch_events
set source_anime_id = null
where source_anime_id is not null
  and not exists (select 1 from anime where anime.id = user_watch_events.source_anime_id);

delete from user_episode_state
where not exists (select 1 from users where users.id = user_episode_state.user_id)
   or not exists (select 1 from anime where anime.id = user_episode_state.anime_id)
   or not exists (select 1 from episodes where episodes.id = user_episode_state.episode_id);

update user_episode_state
set video_source_id = null
where video_source_id is not null
  and not exists (select 1 from video_sources where video_sources.id = user_episode_state.video_source_id);

update user_episode_state
set source_anime_id = null
where source_anime_id is not null
  and not exists (select 1 from anime where anime.id = user_episode_state.source_anime_id);

delete from user_title_state
where not exists (select 1 from users where users.id = user_title_state.user_id)
   or not exists (select 1 from anime where anime.id = user_title_state.anime_id);

create index if not exists idx_video_sources_provider_playable
    on video_sources(provider_id)
    where embed_url is not null;
