create index if not exists idx_content_update_events_type_at
    on content_update_events(event_type, occurred_at desc, id desc);
