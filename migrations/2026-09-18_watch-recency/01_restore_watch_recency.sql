-- Passive load/hide events used to overwrite the timestamp used by Continue
-- Watching. Restore it from actual engagement; do not invent completion or
-- touch manual edits, cleared progress, navigation cursors, or the audit log.
update user_episode_state as state
set last_seen_at = max(
    state.started_at,
    coalesce((
        select max(event.event_at)
        from user_watch_events as event
        where event.user_id = state.user_id
          and event.anime_id = state.anime_id
          and event.episode_id = state.episode_id
          and event.confidence >= 0.65
          and (
              event.event_type in ('player_engaged', 'fullscreen_enter', 'pip_open')
              or (event.event_type = 'heartbeat' and event.engaged_seconds > 0)
          )
    ), state.started_at)
)
where state.started_at is not null
  and state.last_event_type not in ('manual_progress', 'manual_clear');
