create table if not exists catalog_cache_revision (
    singleton integer primary key check (singleton = 1),
    generation integer not null default 0,
    dirty integer not null default 1 check (dirty in (0, 1))
);

insert or ignore into catalog_cache_revision(singleton, generation, dirty)
values (1, 0, 1);

create trigger if not exists catalog_revision_anime_insert
after insert on anime
begin
    update catalog_cache_revision
    set generation = generation + 1,
        dirty = 1
    where singleton = 1 and dirty = 0;
end;

create trigger if not exists catalog_revision_anime_update
after update on anime
begin
    update catalog_cache_revision
    set generation = generation + 1,
        dirty = 1
    where singleton = 1 and dirty = 0;
end;

create trigger if not exists catalog_revision_anime_delete
after delete on anime
begin
    update catalog_cache_revision
    set generation = generation + 1,
        dirty = 1
    where singleton = 1 and dirty = 0;
end;

create trigger if not exists catalog_revision_episodes_insert
after insert on episodes
begin
    update catalog_cache_revision
    set generation = generation + 1,
        dirty = 1
    where singleton = 1 and dirty = 0;
end;

create trigger if not exists catalog_revision_episodes_update
after update on episodes
begin
    update catalog_cache_revision
    set generation = generation + 1,
        dirty = 1
    where singleton = 1 and dirty = 0;
end;

create trigger if not exists catalog_revision_episodes_delete
after delete on episodes
begin
    update catalog_cache_revision
    set generation = generation + 1,
        dirty = 1
    where singleton = 1 and dirty = 0;
end;

create trigger if not exists catalog_revision_video_sources_insert
after insert on video_sources
begin
    update catalog_cache_revision
    set generation = generation + 1,
        dirty = 1
    where singleton = 1 and dirty = 0;
end;

create trigger if not exists catalog_revision_video_sources_update
after update on video_sources
begin
    update catalog_cache_revision
    set generation = generation + 1,
        dirty = 1
    where singleton = 1 and dirty = 0;
end;

create trigger if not exists catalog_revision_video_sources_delete
after delete on video_sources
begin
    update catalog_cache_revision
    set generation = generation + 1,
        dirty = 1
    where singleton = 1 and dirty = 0;
end;

create trigger if not exists catalog_revision_anime_fields_insert
after insert on anime_fields
begin
    update catalog_cache_revision
    set generation = generation + 1,
        dirty = 1
    where singleton = 1 and dirty = 0;
end;

create trigger if not exists catalog_revision_anime_fields_update
after update on anime_fields
begin
    update catalog_cache_revision
    set generation = generation + 1,
        dirty = 1
    where singleton = 1 and dirty = 0;
end;

create trigger if not exists catalog_revision_anime_fields_delete
after delete on anime_fields
begin
    update catalog_cache_revision
    set generation = generation + 1,
        dirty = 1
    where singleton = 1 and dirty = 0;
end;

create trigger if not exists catalog_revision_anime_genres_insert
after insert on anime_genres
begin
    update catalog_cache_revision
    set generation = generation + 1,
        dirty = 1
    where singleton = 1 and dirty = 0;
end;

create trigger if not exists catalog_revision_anime_genres_update
after update on anime_genres
begin
    update catalog_cache_revision
    set generation = generation + 1,
        dirty = 1
    where singleton = 1 and dirty = 0;
end;

create trigger if not exists catalog_revision_anime_genres_delete
after delete on anime_genres
begin
    update catalog_cache_revision
    set generation = generation + 1,
        dirty = 1
    where singleton = 1 and dirty = 0;
end;

create trigger if not exists catalog_revision_anime_dubbings_insert
after insert on anime_dubbings
begin
    update catalog_cache_revision
    set generation = generation + 1,
        dirty = 1
    where singleton = 1 and dirty = 0;
end;

create trigger if not exists catalog_revision_anime_dubbings_update
after update on anime_dubbings
begin
    update catalog_cache_revision
    set generation = generation + 1,
        dirty = 1
    where singleton = 1 and dirty = 0;
end;

create trigger if not exists catalog_revision_anime_dubbings_delete
after delete on anime_dubbings
begin
    update catalog_cache_revision
    set generation = generation + 1,
        dirty = 1
    where singleton = 1 and dirty = 0;
end;

create trigger if not exists catalog_revision_anime_title_aliases_insert
after insert on anime_title_aliases
begin
    update catalog_cache_revision
    set generation = generation + 1,
        dirty = 1
    where singleton = 1 and dirty = 0;
end;

create trigger if not exists catalog_revision_anime_title_aliases_update
after update on anime_title_aliases
begin
    update catalog_cache_revision
    set generation = generation + 1,
        dirty = 1
    where singleton = 1 and dirty = 0;
end;

create trigger if not exists catalog_revision_anime_title_aliases_delete
after delete on anime_title_aliases
begin
    update catalog_cache_revision
    set generation = generation + 1,
        dirty = 1
    where singleton = 1 and dirty = 0;
end;

create trigger if not exists catalog_revision_content_update_events_insert
after insert on content_update_events
begin
    update catalog_cache_revision
    set generation = generation + 1,
        dirty = 1
    where singleton = 1 and dirty = 0;
end;

create trigger if not exists catalog_revision_content_update_events_update
after update on content_update_events
begin
    update catalog_cache_revision
    set generation = generation + 1,
        dirty = 1
    where singleton = 1 and dirty = 0;
end;

create trigger if not exists catalog_revision_content_update_events_delete
after delete on content_update_events
begin
    update catalog_cache_revision
    set generation = generation + 1,
        dirty = 1
    where singleton = 1 and dirty = 0;
end;
