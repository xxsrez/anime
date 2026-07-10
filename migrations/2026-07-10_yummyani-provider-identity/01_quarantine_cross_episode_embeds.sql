-- One playable URL cannot represent two distinct episode selections. Upstream
-- occasionally emits that stale mapping. Delete an ambiguous row only when an
-- unambiguous provider remains for that episode, and move user/event references
-- to that safe replacement before deletion.
create temp table _cross_episode_ambiguous_urls as
select anime_id, embed_url
from video_sources
where embed_url is not null
  and trim(embed_url) <> ''
group by anime_id, embed_url
having count(distinct episode_id) > 1;

create unique index _idx_cross_episode_ambiguous_url
on _cross_episode_ambiguous_urls(anime_id, embed_url);

create temp table _cross_episode_embed_candidates as
select source.id, source.episode_id
from video_sources source
join _cross_episode_ambiguous_urls ambiguous
  on ambiguous.anime_id = source.anime_id
 and ambiguous.embed_url = source.embed_url;

create unique index _idx_cross_episode_candidate_id
on _cross_episode_embed_candidates(id);

create index _idx_cross_episode_candidate_episode
on _cross_episode_embed_candidates(episode_id);

create temp table _cross_episode_embed_deletions as
select candidate.id as candidate_id, min(alternative.id) as replacement_id
from _cross_episode_embed_candidates candidate
join video_sources alternative
  on alternative.episode_id = candidate.episode_id
 and alternative.embed_url is not null
 and trim(alternative.embed_url) <> ''
left join _cross_episode_embed_candidates ambiguous_alternative
  on ambiguous_alternative.id = alternative.id
where ambiguous_alternative.id is null
group by candidate.id;

create unique index _idx_cross_episode_deletion_id
on _cross_episode_embed_deletions(candidate_id);

update user_watch_events
set video_source_id = (
    select replacement_id
    from _cross_episode_embed_deletions
    where candidate_id = user_watch_events.video_source_id
)
where video_source_id in (select candidate_id from _cross_episode_embed_deletions);

update user_episode_state
set video_source_id = (
    select replacement_id
    from _cross_episode_embed_deletions
    where candidate_id = user_episode_state.video_source_id
)
where video_source_id in (select candidate_id from _cross_episode_embed_deletions);

update content_update_events
set video_source_id = (
    select replacement_id
    from _cross_episode_embed_deletions
    where candidate_id = content_update_events.video_source_id
)
where video_source_id in (select candidate_id from _cross_episode_embed_deletions);

delete from video_sources
where id in (select candidate_id from _cross_episode_embed_deletions);

drop table _cross_episode_embed_deletions;
drop table _cross_episode_embed_candidates;
drop table _cross_episode_ambiguous_urls;
