-- Player queries filter out missing embed URLs and never search by URL value.
-- Keep the two lookup indexes small by indexing only the lookup IDs.
drop index if exists idx_video_sources_anime_embed;
drop index if exists idx_video_sources_episode_embed;

create index idx_video_sources_anime_embed
    on video_sources(anime_id) where embed_url is not null;
create index idx_video_sources_episode_embed
    on video_sources(episode_id) where embed_url is not null;
