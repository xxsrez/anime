create table if not exists anime_title_aliases (
    anime_id integer not null references anime(id) on delete cascade,
    alias text not null,
    normalized_alias text not null,
    language text,
    alias_type text not null default 'alias',
    source text not null default 'manual',
    source_ref text,
    confidence real not null default 1.0,
    created_at text not null,
    updated_at text not null,
    primary key (anime_id, normalized_alias, source, alias_type)
);

create index if not exists idx_anime_title_aliases_anime_id
    on anime_title_aliases(anime_id);

create index if not exists idx_anime_title_aliases_normalized
    on anime_title_aliases(normalized_alias);
