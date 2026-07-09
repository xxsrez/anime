create index if not exists idx_anime_fields_external_rating
on anime_fields(lower(trim(label)), anime_id, label, value)
where value is not null
  and lower(trim(label)) in ('tal', 'myanimelist', 'mal', 'anilist', 'shikimori', 'imdb');
