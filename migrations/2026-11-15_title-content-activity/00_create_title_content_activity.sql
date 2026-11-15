create table if not exists title_content_activity (
    anime_id integer primary key references anime(id) on delete cascade,
    last_changed_at text,
    source_updated_at text,
    source_checked_at text,
    source_kind text
);

create trigger if not exists title_content_activity_anime_insert
after insert on anime
begin
    insert into title_content_activity(anime_id, last_changed_at)
    values (new.id, case when julianday(new.scraped_at) is not null then new.scraped_at
                        else strftime('%Y-%m-%dT%H:%M:%fZ', 'now') end)
    on conflict(anime_id) do update set last_changed_at =
        case when julianday(excluded.last_changed_at) > coalesce(julianday(last_changed_at), -1)
             then excluded.last_changed_at else last_changed_at end;
end;

create trigger if not exists title_content_activity_episode_insert
after insert on episodes
begin
    insert into title_content_activity(anime_id, last_changed_at)
    values (new.anime_id, case when julianday(new.scraped_at) is not null then new.scraped_at
                              else strftime('%Y-%m-%dT%H:%M:%fZ', 'now') end)
    on conflict(anime_id) do update set last_changed_at =
        case when julianday(excluded.last_changed_at) > coalesce(julianday(last_changed_at), -1)
             then excluded.last_changed_at else last_changed_at end;
end;

create trigger if not exists title_content_activity_episode_playable
after update of has_video on episodes
when old.has_video != 1 and new.has_video = 1
begin
    insert into title_content_activity(anime_id, last_changed_at)
    values (new.anime_id, case when julianday(new.scraped_at) is not null then new.scraped_at
                              else strftime('%Y-%m-%dT%H:%M:%fZ', 'now') end)
    on conflict(anime_id) do update set last_changed_at =
        case when julianday(excluded.last_changed_at) > coalesce(julianday(last_changed_at), -1)
             then excluded.last_changed_at else last_changed_at end;
end;

create trigger if not exists title_content_activity_provider_insert
after insert on video_sources
when new.embed_url is not null and trim(new.embed_url) != ''
begin
    insert into title_content_activity(anime_id, last_changed_at)
    values (new.anime_id, case when julianday(new.scraped_at) is not null then new.scraped_at
                              else strftime('%Y-%m-%dT%H:%M:%fZ', 'now') end)
    on conflict(anime_id) do update set last_changed_at =
        case when julianday(excluded.last_changed_at) > coalesce(julianday(last_changed_at), -1)
             then excluded.last_changed_at else last_changed_at end;
end;

create trigger if not exists title_content_activity_provider_playable
after update of embed_url on video_sources
when (old.embed_url is null or trim(old.embed_url) = '')
     and new.embed_url is not null and trim(new.embed_url) != ''
begin
    insert into title_content_activity(anime_id, last_changed_at)
    values (new.anime_id, case when julianday(new.scraped_at) is not null then new.scraped_at
                              else strftime('%Y-%m-%dT%H:%M:%fZ', 'now') end)
    on conflict(anime_id) do update set last_changed_at =
        case when julianday(excluded.last_changed_at) > coalesce(julianday(last_changed_at), -1)
             then excluded.last_changed_at else last_changed_at end;
end;
