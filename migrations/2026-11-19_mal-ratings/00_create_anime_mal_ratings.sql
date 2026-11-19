create table if not exists anime_mal_ratings (
    anime_id integer primary key references anime(id) on delete cascade,
    mal_id integer not null check(mal_id > 0),
    score real check(score between 1 and 10),
    scored_by integer check(scored_by > 0),
    checked_at text not null
);

create trigger if not exists catalog_revision_anime_mal_ratings_insert
after insert on anime_mal_ratings
begin
    update catalog_cache_revision
    set generation = generation + 1, dirty = 1
    where singleton = 1 and dirty = 0;
end;

create trigger if not exists catalog_revision_anime_mal_ratings_update
after update on anime_mal_ratings
begin
    update catalog_cache_revision
    set generation = generation + 1, dirty = 1
    where singleton = 1 and dirty = 0;
end;

create trigger if not exists catalog_revision_anime_mal_ratings_delete
after delete on anime_mal_ratings
begin
    update catalog_cache_revision
    set generation = generation + 1, dirty = 1
    where singleton = 1 and dirty = 0;
end;
