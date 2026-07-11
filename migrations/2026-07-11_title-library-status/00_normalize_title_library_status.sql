-- Canonical title-library status: none | watching | completed.
-- Keep historical timestamps and episode telemetry; only active progress is
-- cleared when the title is explicitly outside the watching shelf.
update user_title_state
set watch_status = case
        when coalesce(watched, 0) = 1 or watch_status = 'completed' then 'completed'
        when watch_status in ('watching', 'paused') then 'watching'
        when watch_status is null and progress_episode_number is not null then 'watching'
        else 'none'
    end,
    watched = case
        when coalesce(watched, 0) = 1 or watch_status = 'completed' then 1
        else 0
    end,
    progress_episode_number = case
        when coalesce(watched, 0) = 0
         and coalesce(watch_status, '') not in ('completed', 'watching', 'paused')
         and not (watch_status is null and progress_episode_number is not null)
        then null
        else progress_episode_number
    end,
    not_interested = case
        when coalesce(is_favorite, 0) = 1 then 0
        else coalesce(not_interested, 0)
    end
where watch_status is null
   or watch_status not in ('none', 'watching', 'completed')
   or (coalesce(watched, 0) = 1 and watch_status != 'completed')
   or (watch_status = 'completed' and coalesce(watched, 0) != 1)
   or (watch_status in ('none', 'watching') and coalesce(watched, 0) != 0)
   or (watch_status = 'none' and progress_episode_number is not null)
   or (coalesce(is_favorite, 0) = 1 and coalesce(not_interested, 0) = 1);

update user_episode_state
set started_at = null,
    last_event_type = 'manual_clear',
    last_confidence = 1.0,
    updated_at = coalesce(
        (
            select coalesce(uts.watch_status_updated_at, uts.updated_at)
            from user_title_state uts
            where uts.user_id = user_episode_state.user_id
              and uts.anime_id = user_episode_state.anime_id
        ),
        updated_at
    )
where started_at is not null
  and exists (
      select 1
      from user_title_state uts
      where uts.user_id = user_episode_state.user_id
        and uts.anime_id = user_episode_state.anime_id
        and uts.watch_status = 'none'
  );
