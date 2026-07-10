-- Remove update events that were classified as new translations even though
-- the same Unicode title existed before the run began.
delete from content_update_events
where event_type = 'new_translation'
  and exists (
      select 1
      from content_update_runs r
      join video_sources vs
        on vs.episode_id = content_update_events.episode_id
       and trim(coalesce(vs.translation_title, '')) =
           trim(coalesce(content_update_events.translation_title, ''))
      where r.id = content_update_events.run_id
        and vs.scraped_at < r.started_at
  );

-- Modern YummyAni provider IDs include the upstream video ID and are stable.
-- Stage duplicate groups so old numeric IDs used by deep links and watch state
-- survive while the latest strictly-redacted metadata replaces legacy values.
create temp table _yummyani_provider_groups as
select
    episode_id,
    provider_id,
    coalesce(translation_id, 0) as translation_key,
    min(id) as survivor_id,
    max(id) as latest_id
from video_sources
where provider_id like 'yummyani-%'
group by episode_id, provider_id, coalesce(translation_id, 0)
having count(*) > 1;

create temp table _yummyani_provider_latest as
select
    g.survivor_id,
    g.latest_id,
    latest.provider_title,
    latest.translation_title,
    latest.embed_host,
    latest.embed_url,
    latest.embed_url_redacted,
    latest.scraped_at
from _yummyani_provider_groups g
join video_sources latest on latest.id = g.latest_id;

create unique index _idx_yummyani_provider_latest_survivor
on _yummyani_provider_latest(survivor_id);

create temp table _yummyani_provider_duplicates as
select source.id as duplicate_id, g.survivor_id
from _yummyani_provider_groups g
join video_sources source
  on source.episode_id = g.episode_id
 and source.provider_id = g.provider_id
 and coalesce(source.translation_id, 0) = g.translation_key
where source.id <> g.survivor_id;

create unique index _idx_yummyani_provider_duplicates_id
on _yummyani_provider_duplicates(duplicate_id);

update user_watch_events
set video_source_id = (
    select survivor_id
    from _yummyani_provider_duplicates
    where duplicate_id = user_watch_events.video_source_id
)
where video_source_id in (select duplicate_id from _yummyani_provider_duplicates);

update user_episode_state
set video_source_id = (
    select survivor_id
    from _yummyani_provider_duplicates
    where duplicate_id = user_episode_state.video_source_id
)
where video_source_id in (select duplicate_id from _yummyani_provider_duplicates);

update content_update_events
set video_source_id = (
    select survivor_id
    from _yummyani_provider_duplicates
    where duplicate_id = content_update_events.video_source_id
)
where video_source_id in (select duplicate_id from _yummyani_provider_duplicates);

delete from video_sources
where id in (select duplicate_id from _yummyani_provider_duplicates);

update video_sources
set (
        provider_title,
        translation_title,
        embed_host,
        embed_url,
        embed_url_redacted,
        scraped_at
    ) = (
        select
            provider_title,
            translation_title,
            embed_host,
            embed_url,
            embed_url_redacted,
            scraped_at
        from _yummyani_provider_latest
        where survivor_id = video_sources.id
    )
where id in (select survivor_id from _yummyani_provider_latest);

drop table _yummyani_provider_duplicates;
drop table _yummyani_provider_latest;
drop table _yummyani_provider_groups;

create unique index if not exists idx_video_sources_yummyani_identity
on video_sources(episode_id, provider_id, coalesce(translation_id, 0))
where provider_id like 'yummyani-%';
