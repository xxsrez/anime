-- Normalize already stored source facts; never replace a curated value.
UPDATE anime
SET duration = (
    SELECT value FROM anime_fields
    WHERE anime_id = anime.id AND label IN ('Длительность', 'Время')
      AND nullif(trim(value), '') IS NOT NULL
    ORDER BY CASE label WHEN 'Длительность' THEN 0 ELSE 1 END
    LIMIT 1
)
WHERE nullif(trim(duration), '') IS NULL
  AND EXISTS (
    SELECT 1 FROM anime_fields
    WHERE anime_id = anime.id AND label IN ('Длительность', 'Время')
      AND nullif(trim(value), '') IS NOT NULL
  );
