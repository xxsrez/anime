update anime
set source_id = 'yummyani:' || source_id
where source = 'yummyanime'
  and id >= 20000000
  and source_id not like 'yummyani:%';

update content_update_events
set source_id = 'yummyani:' || source_id
where source = 'yummyanime'
  and anime_id >= 20000000
  and source_id is not null
  and source_id not like 'yummyani:%';

create unique index if not exists idx_anime_source_source_id_unique
    on anime(source, source_id)
    where source is not null and source_id is not null;
