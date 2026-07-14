#!/usr/bin/env python3
import datetime as dt
import http.client
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
from urllib.parse import parse_qs, urlencode, urlparse
from unittest.mock import patch

import backfill_players
import content_updates
import scrape_animego
import scrape_yummyanime
import server
import sync_videos
from scripts import enrich_title_aliases


class LocalAppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._original_default_db = server.DEFAULT_DB
        cls._catalog_tmpdir = tempfile.TemporaryDirectory()
        fixture_path = Path(cls._catalog_tmpdir.name) / "catalog-fixture.sqlite"
        cls.build_catalog_fixture(fixture_path)
        server.DEFAULT_DB = fixture_path
        server.invalidate_catalog_cache(fixture_path)

    @classmethod
    def tearDownClass(cls):
        server.invalidate_catalog_cache(server.DEFAULT_DB)
        server.reset_database_initialization(server.DEFAULT_DB)
        server.DEFAULT_DB = cls._original_default_db
        cls._catalog_tmpdir.cleanup()
        super().tearDownClass()

    @classmethod
    def build_catalog_fixture(cls, db_path):
        con = scrape_animego.init_db(db_path)
        scraped_at = "2026-07-01T00:00:00+00:00"

        def add_title(
            anime_id,
            title,
            *,
            subtitle=None,
            source="animego",
            source_id=None,
            year="2026",
            episode_count=1,
            aggregate_score=7.2,
            aggregate_count=120,
            genre="Фэнтези",
        ):
            con.execute(
                """
                insert into anime(
                    id, slug, title, subtitle, url, kind, year, status,
                    episodes_text, scraped_at, aggregate_score, aggregate_count,
                    date_published, source, source_id
                ) values (?, ?, ?, ?, ?, 'TV', ?, 'Завершён', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    anime_id,
                    f"fixture-{anime_id}",
                    title,
                    subtitle,
                    f"https://example.test/anime/{anime_id}",
                    year,
                    str(episode_count),
                    scraped_at,
                    aggregate_score,
                    aggregate_count,
                    f"{year}-01-01" if year else None,
                    source,
                    source_id or str(anime_id),
                ),
            )
            con.execute("insert into anime_genres(anime_id, genre) values (?, ?)", (anime_id, genre))
            for label, value in (
                ("Тип", "TV"),
                ("Статус", "Завершён"),
                ("Источник", source),
            ):
                con.execute(
                    "insert into anime_fields(anime_id, label, value) values (?, ?, ?)",
                    (anime_id, label, value),
                )
            for number in range(1, episode_count + 1):
                episode_id = anime_id * 100 + number
                con.execute(
                    """
                    insert into episodes(id, anime_id, number, has_video, scraped_at)
                    values (?, ?, ?, 1, ?)
                    """,
                    (episode_id, anime_id, str(number), scraped_at),
                )
                con.execute(
                    """
                    insert into video_sources(
                        anime_id, episode_id, provider_id, provider_title,
                        translation_id, translation_title, embed_host,
                        embed_url, embed_url_redacted, scraped_at
                    ) values (?, ?, ?, 'Kodik', 1, 'Dream Cast', 'kodikplayer.com', ?, ?, ?)
                    """,
                    (
                        anime_id,
                        episode_id,
                        f"kodik-{anime_id}-{number}",
                        f"https://kodikplayer.com/serial/{anime_id}/fixture/720p?season=1&episode={number}",
                        f"https://kodikplayer.com/serial/{anime_id}/<redacted>/720p?season=1&episode={number}",
                        scraped_at,
                    ),
                )

        special = (
            (101, "Легенда о Хэй", "Luo Xiaohei Zhan Ji"),
            (102, "Ванпанчмен", "One Punch Man"),
            (103, "Реинкарнация сильнейшего оммёдзи", "Saikyou Onmyouji no Isekai Tenseiki"),
            (10001570, "Как поживаете?", "Kimitachi wa Dou Ikiru ka"),
        )
        for anime_id, title, subtitle in special:
            add_title(anime_id, title, subtitle=subtitle)
        con.execute(
            "insert into anime_fields(anime_id, label, value) values (10001570, 'Режиссер', 'Хаяо Миядзаки')"
        )

        add_title(10005287, "WIXOSS Diva(A)Live", subtitle="WIXOSS", aggregate_score=10.0, aggregate_count=2)
        con.execute(
            "insert into anime_fields(anime_id, label, value) values (10005287, 'Shikimori', '5.6')"
        )
        add_title(2430, "Провожающая в последний путь Фрирен", subtitle="Sousou no Frieren", episode_count=28)
        add_title(2911, "Фрирен: продолжение", subtitle="Sousou no Frieren 2", episode_count=10, year="2025")

        for index in range(10):
            title = f"Canonical Pair {index}"
            subtitle = f"Canonical Pair Romaji {index}"
            add_title(5000 + index, title, subtitle=subtitle, year="2025")
            add_title(
                10005000 + index,
                title,
                subtitle=subtitle,
                source="yummyanime",
                year="2025",
            )

        add_title(2463, "Лунное путешествие приведёт к новому миру 2", subtitle="Tsuki ga Michibiku Isekai Douchuu 2", year="2025")
        add_title(
            10001210,
            "Цукимити 2",
            subtitle="Tsuki ga Michibiku Isekai Douchuu 2",
            source="yummyanime",
            year="2025",
        )

        for index in range(24):
            add_title(
                6000 + index,
                f"Fixture Catalog Title {index}",
                subtitle=f"Fixture Title Romaji {index}",
                year="2025" if index % 2 else "2026",
                genre="Приключения" if index % 3 else "Комедия",
            )

        for anime_id, title, episode_count in (
            (10009001, "Реинкарнация безработного 3 сезон", 2),
            (10009002, "Yummy Fixture One", 1),
            (10009003, "Yummy Fixture Two", 1),
            (10009004, "Yummy Fixture Three", 1),
        ):
            add_title(
                anime_id,
                title,
                subtitle=f"Yummy Fixture {anime_id}",
                source="yummyanime",
                episode_count=episode_count,
            )

        con.commit()
        con.close()
        server.prepare_database(db_path)

    def create_google_user(self, db_path, sub, email):
        con = server.connect(db_path)
        try:
            user = server.upsert_google_user(
                con,
                {
                    "sub": sub,
                    "email": email,
                    "email_verified": True,
                    "name": email.split("@")[0],
                    "picture": None,
                },
            )
            con.commit()
            return user["id"]
        finally:
            con.close()

    def create_session(self, db_path, user_id):
        con = server.connect(db_path)
        try:
            token, _ = server.create_session(con, user_id)
            con.commit()
            return token
        finally:
            con.close()

    def request_test_server(self, db_path, method, path, headers=None, body=None):
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.AnimeHandler)
        httpd.db_path = db_path
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            body = response.read()
            return response.status, dict(response.getheaders()), body
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def latest_json_log_entry(self, log_dir, filename):
        entries = self.json_log_entries(log_dir, filename)
        self.assertTrue(entries)
        return entries[-1]

    def json_log_entries(self, log_dir, filename):
        lines = (Path(log_dir) / filename).read_text(encoding="utf-8").strip().splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def test_load_env_file_sets_missing_values_without_overriding_existing_env(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "ANIME_TEST_ENV_LOADER_VALUE=loaded",
                        "ANIME_TEST_ENV_LOADER_QUOTED=\"quoted value\"",
                        "ANIME_TEST_ENV_LOADER_KEEP=file-value",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"ANIME_TEST_ENV_LOADER_KEEP": "existing"}):
                self.assertTrue(server.load_env_file(env_path))
                self.assertEqual(os.environ["ANIME_TEST_ENV_LOADER_VALUE"], "loaded")
                self.assertEqual(os.environ["ANIME_TEST_ENV_LOADER_QUOTED"], "quoted value")
                self.assertEqual(os.environ["ANIME_TEST_ENV_LOADER_KEEP"], "existing")

    def source_row(self, translation, provider="Kodik", episode_id=1, source="animego", row_id=1):
        return {
            "id": row_id,
            "episode_id": episode_id,
            "source": source,
            "translation_title": translation,
            "provider_title": provider,
            "embed_host": "kodikplayer.com" if provider.startswith("Kodik") else "example.test",
        }

    def seed_watchable_title(self, db_path, anime_id=1, title="Smoke Title", episode_count=1):
        con = scrape_animego.init_db(db_path)
        try:
            scraped_at = server.now_iso()
            con.execute(
                """
                insert into anime(id, slug, title, url, source, source_id, year, scraped_at)
                values (?, ?, ?, ?, 'animego', ?, '2026', ?)
                """,
                (
                    anime_id,
                    f"smoke-title-{anime_id}",
                    title,
                    f"https://animego.me/anime/smoke-title-{anime_id}",
                    str(anime_id),
                    scraped_at,
                ),
            )
            for episode_number in range(1, episode_count + 1):
                episode_id = anime_id * 1000 + episode_number
                con.execute(
                    """
                    insert into episodes(id, anime_id, number, has_video, scraped_at)
                    values (?, ?, ?, 1, ?)
                    """,
                    (episode_id, anime_id, str(episode_number), scraped_at),
                )
                con.execute(
                    """
                    insert into video_sources(
                        anime_id, episode_id, provider_id, provider_title,
                        translation_id, translation_title, embed_url, embed_url_redacted,
                        embed_host, scraped_at
                    )
                    values (?, ?, 'kodik', 'Kodik', 1, 'Dream Cast', ?, ?, 'kodikplayer.com', ?)
                    """,
                    (
                        anime_id,
                        episode_id,
                        f"https://kodikplayer.com/serial/{anime_id}/hash/720p?season=1&episode={episode_number}",
                        f"https://kodikplayer.com/serial/{anime_id}/<redacted>/720p",
                        scraped_at,
                    ),
                )
            con.commit()
        finally:
            con.close()
        server.invalidate_catalog_cache(db_path)
        return anime_id

    def test_catalog_has_scraped_titles(self):
        items = server.get_anime_list()
        self.assertGreaterEqual(len(items), 10)
        self.assertTrue(all(item["source_count"] > 0 for item in items))
        self.assertTrue(all(item["available_episode_count"] > 0 for item in items))
        slugs = [item["slug"] for item in items]
        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertTrue(all(item["internal_id"] == item["slug"] for item in items))
        self.assertTrue(all("-" in item["slug"] for item in items))

    def test_frontend_search_scorer_handles_typos_and_variant_titles(self):
        script = r"""
const assert = require("assert");
const search = require("./static/search.js");
const items = search.prepareSearchIndexes([
  {
    id: "hei",
    title: "Легенда о Хэй",
    subtitle: "Luo Xiaohei Zhan Ji",
    genres: ["Фэнтези"],
    source_variants: [],
  },
  {
    id: "opm",
    title: "Ванпанчмен",
    subtitle: "One Punch Man",
    genres: ["Экшен"],
    source_variants: [],
  },
  {
    id: "onmyo",
    title: "Реинкарнация сильнейшего оммёдзи",
    subtitle: "Saikyou Onmyouji no Isekai Tenseiki",
    genres: ["Исэкай"],
    source_variants: [],
  },
  {
    id: "moon",
    title: "Лунное путешествие приведёт к новому миру",
    subtitle: "Tsuki ga Michibiku Isekai Douchuu",
    genres: ["Приключения"],
    source_variants: [
      {title: "Moonlit Fantasy", subtitle: "Tsukimichi", source: "yummyanime"},
    ],
  },
  {
    id: "heron",
    title: "Как поживаете?",
    subtitle: "Kimitachi wa Dou Ikiru ka",
    genres: ["Приключения"],
    source_variants: [],
    search_fields: [
      {value: "Мальчик и херон", weight: 9, kind: "alias"},
      {value: "The Boy and the Heron", weight: 8, kind: "alias"},
      {value: "君たちはどう生きるか", weight: 8, kind: "alias"},
      {value: "Хаяо Миядзаки", weight: 5, kind: "metadata"},
    ],
  },
  {
    id: "miyazaki-title",
    title: "Миядзаки: тестовый тайтл",
    subtitle: null,
    genres: [],
    source_variants: [],
  },
  {
    id: "short-miya",
    title: "Короткое имя",
    subtitle: null,
    genres: [],
    source_variants: [],
    search_fields: [
      {value: "Сигэюки Мия", weight: 5, kind: "metadata"},
    ],
  },
  {
    id: "imadzaki",
    title: "Другая фамилия",
    subtitle: null,
    genres: [],
    source_variants: [],
    search_fields: [
      {value: "Ицуки Имадзаки", weight: 5, kind: "metadata"},
    ],
  },
  {
    id: "production-magic",
    title: "Весёлая защита владений беспечного лорда",
    subtitle: "Okiraku Ryoushu no Tanoshii Ryouchi Bouei",
    genres: ["Фэнтези"],
    source_variants: [],
    search_fields: [
      {
        value: "Весёлая защита владений беспечного лорда: Превращение безымянной деревни в неприступную крепость с помощью производственной магии",
        weight: 9,
        kind: "alias",
      },
    ],
  },
  {
    id: "generic-magic",
    title: "Постороннее аниме",
    subtitle: null,
    genres: ["Магия"],
    source_variants: [],
  },
]);

function rankedIds(queryText) {
  const query = search.searchQuery(queryText);
  return items
    .map((item, index) => ({item, index, score: search.scoreSearchItem(item, query)}))
    .filter(entry => entry.score > 0)
    .sort((left, right) => right.score - left.score || left.index - right.index)
    .map(entry => entry.item.id);
}

assert.strictEqual(search.searchText("Хэй"), search.searchText("хей"));
assert.strictEqual(rankedIds("легенда хей")[0], "hei");
assert.strictEqual(rankedIds("one punc man")[0], "opm");
assert.strictEqual(rankedIds("реинкарнация омедзи")[0], "onmyo");
assert.strictEqual(rankedIds("moonlit")[0], "moon");
assert.strictEqual(rankedIds("мальчик херон")[0], "heron");
assert.strictEqual(rankedIds("the boy heron")[0], "heron");
assert.strictEqual(rankedIds("君たちはどう生きるか")[0], "heron");
assert.strictEqual(rankedIds("хаяо миядзаки")[0], "heron");
assert.strictEqual(rankedIds("миядзаки")[0], "miyazaki-title");
assert.strictEqual(rankedIds("сигэюки мия")[0], "short-miya");
assert.strictEqual(rankedIds("ицуки имадзаки")[0], "imadzaki");
assert.strictEqual(rankedIds("производственная магия")[0], "production-magic");
assert.ok(!rankedIds("производственная магия").includes("generic-magic"));
assert.ok(!rankedIds("миядзаки").includes("short-miya"));
assert.ok(!rankedIds("миядзаки").includes("imadzaki"));
assert.deepStrictEqual(rankedIds("zz"), []);
"""
        result = subprocess.run(
            ["node", "-e", script],
            cwd=server.ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_api_search_query_is_fuzzy_and_ranked(self):
        hei = server.get_anime_list(q="легенда хей")
        self.assertTrue(hei)
        self.assertIn("Хэй", hei[0]["title"])

        one_punch = server.get_anime_list(q="one punc man")
        self.assertTrue(one_punch)
        self.assertIn("One Punch Man", one_punch[0]["subtitle"])

        onmyo = server.get_anime_list(q="реинкарнация омедзи")
        self.assertTrue(onmyo)
        self.assertIn("оммёдзи", onmyo[0]["title"])

        heron = server.get_anime_list(q="мальчик херон")
        self.assertTrue(heron)
        self.assertEqual(heron[0]["id"], 10001570)

        miyazaki = server.get_anime_list(q="хаяо миядзаки")
        self.assertTrue(miyazaki)
        self.assertEqual(miyazaki[0]["id"], 10001570)

        production_magic_query = server.search_query_info("производственная магия")
        production_magic = {
            "title": "Весёлая защита владений беспечного лорда",
            "subtitle": "Okiraku Ryoushu no Tanoshii Ryouchi Bouei",
            "genres": ["Фэнтези"],
            "search_fields": [
                {
                    "value": (
                        "Весёлая защита владений беспечного лорда: Превращение безымянной "
                        "деревни в неприступную крепость с помощью производственной магии"
                    ),
                    "weight": 9,
                    "kind": "alias",
                }
            ],
        }
        generic_magic = {
            "title": "Постороннее аниме",
            "genres": ["Магия"],
        }
        self.assertGreater(
            server.item_search_score(production_magic, production_magic_query),
            server.item_search_score(generic_magic, production_magic_query),
        )
        self.assertEqual(server.item_search_score(generic_magic, production_magic_query), 0)

    def test_api_search_uses_aliases_and_selected_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            self.seed_watchable_title(db_path, anime_id=1, title="Миядзаки: тестовый тайтл")
            self.seed_watchable_title(db_path, anime_id=2, title="Как поживаете?")
            self.seed_watchable_title(db_path, anime_id=3, title="Шумная карточка")
            self.seed_watchable_title(db_path, anime_id=4, title="Короткое имя")
            self.seed_watchable_title(db_path, anime_id=5, title="Другая фамилия")

            con = server.connect(db_path)
            try:
                timestamp = server.now_iso()
                con.execute(
                    "insert into anime_fields(anime_id, label, value) values (2, 'Режиссер', 'Хаяо Миядзаки')"
                )
                con.execute(
                    "insert into anime_fields(anime_id, label, value) values (2, 'Студия', 'Studio Ghibli')"
                )
                con.execute(
                    "insert into anime_fields(anime_id, label, value) values (3, 'Главные герои', 'Иссэй Миядзаки')"
                )
                con.execute(
                    "insert into anime_fields(anime_id, label, value) values (4, 'Режиссер', 'Сигэюки Мия')"
                )
                con.execute(
                    "insert into anime_fields(anime_id, label, value) values (5, 'Режиссер', 'Ицуки Имадзаки')"
                )
                for alias, alias_type in [
                    ("Мальчик и херон", "manual"),
                    ("The Boy and the Heron", "english"),
                    ("君たちはどう生きるか", "native"),
                ]:
                    con.execute(
                        """
                        insert into anime_title_aliases (
                            anime_id, alias, normalized_alias, language, alias_type,
                            source, created_at, updated_at
                        ) values (2, ?, ?, null, ?, 'test', ?, ?)
                        """,
                        (alias, server.normalize_search_text(alias), alias_type, timestamp, timestamp),
                    )
                con.commit()
            finally:
                con.close()
            server.invalidate_catalog_cache(db_path)

            self.assertEqual(server.get_anime_list(db_path, q="мальчик херон")[0]["title"], "Как поживаете?")
            self.assertEqual(server.get_anime_list(db_path, q="the boy heron")[0]["title"], "Как поживаете?")
            self.assertEqual(server.get_anime_list(db_path, q="君たちはどう生きるか")[0]["title"], "Как поживаете?")
            self.assertEqual(server.get_anime_list(db_path, q="хаяо миядзаки")[0]["title"], "Как поживаете?")
            self.assertEqual(server.get_anime_list(db_path, q="режиссер хаяо миядзаки")[0]["title"], "Как поживаете?")
            self.assertEqual(server.get_anime_list(db_path, q="миядзаки")[0]["title"], "Миядзаки: тестовый тайтл")
            self.assertEqual(server.get_anime_list(db_path, q="сигэюки мия")[0]["title"], "Короткое имя")
            self.assertEqual(server.get_anime_list(db_path, q="ицуки имадзаки")[0]["title"], "Другая фамилия")
            short_name_hits = [
                item for item in server.get_anime_list(db_path, q="миядзаки")
                if item["title"] == "Короткое имя"
            ]
            self.assertEqual(short_name_hits, [])
            imadzaki_hits = [
                item for item in server.get_anime_list(db_path, q="миядзаки")
                if item["title"] == "Другая фамилия"
            ]
            self.assertEqual(imadzaki_hits, [])
            noisy_hits = [
                item for item in server.get_anime_list(db_path, q="иссэй миядзаки")
                if item["title"] == "Шумная карточка"
            ]
            self.assertEqual(noisy_hits, [])

    def test_title_alias_enricher_matches_aod_and_builds_anidb_aliases(self):
        rows = [
            {
                "id": 2,
                "title": "Как поживаете?",
                "subtitle": "Kimitachi wa Dou Ikiru ka",
                "year": "2023",
                "episodes_text": "1",
                "kind": "Фильм",
                "source": "yummyanime",
            }
        ]
        aod_entries = [
            {
                "title": "Kimitachi wa Dou Ikiru ka",
                "synonyms": ["The Boy and the Heron"],
                "animeSeason": {"year": 2023},
                "episodes": 1,
                "sources": [
                    "https://myanimelist.net/anime/36699",
                    "https://anidb.net/anime/13545",
                    "https://anilist.co/anime/109979",
                ],
            },
            {
                "title": "Kimitachi wa Dou Ikiru ka",
                "synonyms": [],
                "animeSeason": {"year": 1999},
                "episodes": 1,
                "sources": ["https://myanimelist.net/anime/1"],
            },
        ]

        result = enrich_title_aliases.match_aod_entries(rows, aod_entries)
        self.assertEqual(result["matches"][2]["mal_id"], 36699)
        self.assertEqual(result["matches"][2]["anidb_id"], 13545)
        self.assertEqual(result["ambiguous"], {})

        aliases, missing = enrich_title_aliases.build_anidb_aliases(
            {2: rows[0]},
            result["matches"],
            {
                13545: {
                    "id": 13545,
                    "titles": [
                        {"language": "x-jat", "type": "main", "title": "Kimitachi wa Dou Ikiru ka"},
                        {"language": "en", "type": "official", "title": "The Boy and the Heron"},
                        {"language": "ja", "type": "official", "title": "君たちはどう生きるか"},
                        {"language": "ru", "type": "official", "title": "Мальчик и птица"},
                    ],
                }
            },
        )

        self.assertEqual(missing, 0)
        alias_values = {alias["alias"]: alias for alias in aliases}
        self.assertNotIn("Kimitachi wa Dou Ikiru ka", alias_values)
        self.assertEqual(alias_values["The Boy and the Heron"]["language"], "en")
        self.assertEqual(alias_values["君たちはどう生きるか"]["alias_type"], "native")
        self.assertEqual(alias_values["Мальчик и птица"]["language"], "ru")

    def test_title_detail_can_be_loaded_by_slug(self):
        item = server.get_anime_list()[0]
        detail = server.get_anime_detail(item["slug"])
        self.assertIsNotNone(detail)
        self.assertEqual(detail["id"], item["id"])
        self.assertEqual(detail["slug"], item["slug"])
        self.assertEqual(detail["internal_id"], item["slug"])

    def test_detail_contains_episode_sources(self):
        anime = next(item for item in server.get_anime_list() if item["source_count"] > 0)
        detail = server.get_anime_detail(anime["id"])
        self.assertIsNotNone(detail)
        self.assertGreater(len(detail["episodes"]), 0)
        source_rows = [
            source
            for sources in detail["sources_by_episode"].values()
            for source in sources
        ]
        self.assertTrue(any(source["embed_url"] for source in source_rows))
        self.assertTrue(any(source["translation_title"] for source in source_rows))

    def test_backfill_selects_partial_episode_coverage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            con = backfill_players.connect(db_path)
            try:
                scraped_at = server.now_iso()
                con.execute(
                    """
                    insert into anime(id, slug, title, url, source, source_id, episodes_text, scraped_at)
                    values (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (1, "partial", "Partial", "https://animego.me/anime/partial-1", "animego", "1", "3", scraped_at),
                )
                con.execute(
                    """
                    insert into anime(id, slug, title, url, source, source_id, episodes_text, scraped_at)
                    values (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (2, "complete", "Complete", "https://animego.me/anime/complete-2", "animego", "2", "1", scraped_at),
                )
                for anime_id, episode_id in ((1, 101), (2, 201)):
                    con.execute(
                        """
                        insert into episodes(id, anime_id, number, has_video, scraped_at)
                        values (?, ?, ?, 1, ?)
                        """,
                        (episode_id, anime_id, "1", scraped_at),
                    )
                    con.execute(
                        """
                        insert into video_sources(anime_id, episode_id, provider_id, translation_id, embed_url, scraped_at)
                        values (?, ?, 'kodik', 1, ?, ?)
                        """,
                        (anime_id, episode_id, f"https://example.test/{episode_id}", scraped_at),
                    )
                con.commit()

                rows = backfill_players.playable_missing_rows(con, source="animego")
                self.assertEqual([row["id"] for row in rows], [1])

                forced_rows = backfill_players.playable_missing_rows(con, source="animego", anime_ids=[2])
                self.assertEqual([row["id"] for row in forced_rows], [2])
            finally:
                con.close()

    def test_animego_episode_limit_zero_selects_all(self):
        episodes = [{"id": 1}, {"id": 2}, {"id": 3}]
        self.assertEqual(scrape_animego.selected_episodes(episodes, 0), episodes)
        self.assertEqual(scrape_animego.selected_episodes(episodes, 2), episodes[:2])

    def test_prepare_database_creates_empty_catalog_for_fresh_clone(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "nested" / "animego.sqlite"

            prepared = server.prepare_database(db_path)

            self.assertEqual(prepared, db_path)
            self.assertTrue(db_path.exists())
            self.assertEqual(server.get_anime_list(db_path), [])

            status, _, body = self.request_test_server(str(db_path), "GET", "/api/health")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"ok": True})

            status, headers, _ = self.request_test_server(str(db_path), "GET", "/")
            self.assertEqual(status, 302)
            self.assertTrue(headers["Location"].startswith("/login?next="))

            status, _, body = self.request_test_server(str(db_path), "GET", "/login")
            self.assertEqual(status, 200)
            self.assertIn(b"Anime Catalog", body)

    def test_recent_update_events_are_exposed_in_list_and_detail(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            anime_id = self.seed_watchable_title(db_path, anime_id=91, title="Fresh Update")
            con = server.connect(db_path)
            try:
                content_updates.insert_event(
                    con,
                    None,
                    "new_episode",
                    anime_id,
                    episode_id=anime_id * 1000 + 1,
                    source="animego",
                    source_id=str(anime_id),
                    episode_number="1",
                    title="Fresh Update",
                    description="Добавлена серия 1",
                    metadata={"provider_count": 1},
                    occurred_at=(
                        dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=6)
                    ).isoformat(timespec="seconds"),
                )
                con.commit()
            finally:
                con.close()
            server.invalidate_catalog_cache(db_path)

            item = next(entry for entry in server.get_anime_list(db_path) if entry["id"] == anime_id)
            self.assertEqual(item["recent_update_summary"]["badge"], "+1 серия")
            self.assertEqual(item["recent_update_summary"]["days"], 7)
            self.assertEqual(item["recent_updates"][0]["event_type"], "new_episode")

            detail = server.get_anime_detail(anime_id, db_path)
            self.assertEqual(detail["recent_update_summary"]["label"], "Добавлено 1 серия")
            self.assertEqual(detail["recent_updates"][0]["episode_number"], "1")

    def test_content_updates_api_summarizes_new_database_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            first_id = self.seed_watchable_title(db_path, anime_id=91, title="Fresh Title", episode_count=2)
            second_id = self.seed_watchable_title(db_path, anime_id=92, title="Fresh Voice", episode_count=1)
            old_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).isoformat(timespec="seconds")
            con = server.connect(db_path)
            try:
                content_updates.insert_event(
                    con,
                    None,
                    "new_title",
                    first_id,
                    source="animego",
                    source_id=str(first_id),
                    title="Fresh Title",
                    description="Новый тайтл",
                )
                content_updates.insert_event(
                    con,
                    None,
                    "new_episode",
                    first_id,
                    episode_id=first_id * 1000 + 2,
                    source="animego",
                    source_id=str(first_id),
                    episode_number="2",
                    title="Fresh Title",
                    description="Добавлена серия 2",
                )
                content_updates.insert_event(
                    con,
                    None,
                    "new_translation",
                    second_id,
                    episode_id=second_id * 1000 + 1,
                    source="animego",
                    source_id=str(second_id),
                    episode_number="1",
                    translation_title="Dream Cast",
                    title="Fresh Voice",
                    description="Добавлена озвучка Dream Cast",
                )
                content_updates.insert_event(
                    con,
                    None,
                    "new_provider",
                    second_id,
                    episode_id=second_id * 1000 + 1,
                    source="animego",
                    source_id=str(second_id),
                    episode_number="1",
                    provider_title="Kodik",
                    title="Fresh Voice",
                    description="Старый плеер",
                    occurred_at=old_at,
                )
                con.commit()
            finally:
                con.close()
            server.invalidate_catalog_cache(db_path)

            payload = server.get_content_updates(db_path, days=7, limit=20)

            self.assertEqual(payload["period"]["days"], 7)
            self.assertEqual(payload["summary"]["event_count"], 3)
            self.assertEqual(payload["summary"]["updated_title_count"], 2)
            self.assertEqual(payload["summary"]["event_counts"]["new_title"], 1)
            self.assertEqual(payload["summary"]["event_counts"]["new_episode"], 1)
            self.assertEqual(payload["summary"]["event_counts"]["new_translation"], 1)
            self.assertEqual(payload["summary"]["event_counts"]["new_provider"], 0)
            self.assertEqual({item["title"] for item in payload["items"]}, {"Fresh Title", "Fresh Voice"})
            self.assertNotIn("Старый плеер", {event.get("description") for event in payload["events"]})
            self.assertEqual(
                payload["pagination"],
                {"limit": 20, "returned": 2, "returned_events": 3, "has_more": False},
            )

            limited = server.get_content_updates(db_path, days=7, limit=2)
            self.assertEqual(
                limited["pagination"],
                {"limit": 2, "returned": 2, "returned_events": 3, "has_more": False},
            )

            user_id = self.create_google_user(db_path, "google-user-updates", "updates@example.com")
            token = self.create_session(db_path, user_id)
            status, _, body = self.request_test_server(
                db_path,
                "GET",
                "/api/content-updates?days=7&limit=20",
                headers={"Cookie": f"{server.SESSION_COOKIE_NAME}={token}"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["summary"]["event_count"], 3)

    def test_internal_daily_sync_requires_token_and_runs_wrapper(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            self.seed_watchable_title(db_path, anime_id=92, title="Cron Sync")
            with patch.dict(os.environ, {"ANIME_SYNC_TOKEN": "secret"}, clear=False):
                status, _, body = self.request_test_server(db_path, "POST", "/api/internal/daily-sync?mode=daily")
                self.assertEqual(status, 401)
                self.assertIn("authentication required", body.decode("utf-8"))

                with patch.object(
                    server,
                    "run_content_sync",
                    return_value={
                        "event": "content_sync",
                        "mode": "daily",
                        "trigger": "railway-cron",
                        "duration_ms": 12,
                        "stats": {"animego": {"known_skipped": 1}},
                        "timestamp": server.now_iso(),
                    },
                ) as sync_mock:
                    status, _, body = self.request_test_server(
                        db_path,
                        "POST",
                        "/api/internal/daily-sync?mode=daily",
                        headers={"Authorization": "Bearer secret"},
                    )
                    self.assertEqual(status, 200)
                    payload = json.loads(body)
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["duration_ms"], 12)
                    sync_mock.assert_called_once()

                partial_event = {
                    "event": "content_sync",
                    "status": "partial",
                    "mode": "daily",
                    "trigger": "railway-cron",
                    "duration_ms": 12,
                    "stats": {"animego": {"failed": 1}},
                    "timestamp": server.now_iso(),
                }
                with patch.object(
                    server,
                    "run_content_sync",
                    side_effect=server.ContentSyncPartialError(partial_event),
                ):
                    status, _, body = self.request_test_server(
                        db_path,
                        "POST",
                        "/api/internal/daily-sync?mode=daily",
                        headers={"Authorization": "Bearer secret"},
                    )
                self.assertEqual(status, 502)
                self.assertFalse(json.loads(body)["ok"])
                self.assertEqual(json.loads(body)["status"], "partial")

                with patch.object(
                    server,
                    "run_content_sync",
                    side_effect=server.ContentSyncBusyError("content sync is already running"),
                ):
                    status, _, body = self.request_test_server(
                        db_path,
                        "POST",
                        "/api/internal/daily-sync?mode=daily",
                        headers={"Authorization": "Bearer secret"},
                    )
                self.assertEqual(status, 423)
                self.assertEqual(json.loads(body)["status"], "busy")
                self.assertFalse(json.loads(body)["ok"])

    def test_internal_animego_push_endpoints_require_token_and_use_wrapper(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            self.seed_watchable_title(db_path, anime_id=93, title="Trusted Push")
            manifest = {"version": 1, "items": [], "sync_state": {}, "generated_at": server.now_iso()}
            event = {
                "event": "content_sync_push",
                "source": "animego",
                "status": "success",
                "mode": "daily",
                "trigger": "trusted-animego-worker",
                "duration_ms": 5,
                "stats": {"animego": {"failed": 0}},
                "timestamp": server.now_iso(),
            }
            bundle = {"version": 1, "source": "animego", "mode": "daily", "snapshots": []}
            encoded_bundle = json.dumps(bundle).encode("utf-8")
            with patch.dict(os.environ, {"ANIMEGO_PUSH_TOKEN": "secret"}, clear=False):
                status, _, _ = self.request_test_server(
                    db_path,
                    "GET",
                    "/api/internal/animego-sync-manifest",
                )
                self.assertEqual(status, 401)

                with patch.object(sync_videos, "animego_sync_manifest", return_value=manifest) as manifest_mock:
                    status, _, body = self.request_test_server(
                        db_path,
                        "GET",
                        "/api/internal/animego-sync-manifest",
                        headers={"Authorization": "Bearer secret"},
                    )
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), manifest)
                manifest_mock.assert_called_once_with(db_path)

                status, _, _ = self.request_test_server(
                    db_path,
                    "POST",
                    "/api/internal/animego-push-sync",
                    headers={"Content-Type": "application/json"},
                    body=encoded_bundle,
                )
                self.assertEqual(status, 401)

                with patch.object(server, "run_pushed_animego_sync", return_value=event) as push_mock:
                    status, _, body = self.request_test_server(
                        db_path,
                        "POST",
                        "/api/internal/animego-push-sync",
                        headers={
                            "Authorization": "Bearer secret",
                            "Content-Type": "application/json",
                            "Content-Length": str(len(encoded_bundle)),
                        },
                        body=encoded_bundle,
                    )
                self.assertEqual(status, 200)
                self.assertTrue(json.loads(body)["ok"])
                push_mock.assert_called_once_with(db_path, bundle)

                with patch.object(server, "run_pushed_animego_sync", side_effect=ValueError("bad bundle")):
                    status, _, body = self.request_test_server(
                        db_path,
                        "POST",
                        "/api/internal/animego-push-sync",
                        headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
                        body=encoded_bundle,
                    )
                self.assertEqual(status, 400)
                self.assertIn("bad bundle", body.decode("utf-8"))

                with patch.object(
                    server,
                    "run_pushed_animego_sync",
                    side_effect=server.ContentSyncBusyError("content sync is already running"),
                ):
                    status, _, body = self.request_test_server(
                        db_path,
                        "POST",
                        "/api/internal/animego-push-sync",
                        headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
                        body=encoded_bundle,
                    )
                self.assertEqual(status, 423)
                self.assertEqual(json.loads(body)["status"], "busy")

                partial = {**event, "status": "partial"}
                with patch.object(
                    server,
                    "run_pushed_animego_sync",
                    side_effect=server.ContentSyncPartialError(partial),
                ):
                    status, _, body = self.request_test_server(
                        db_path,
                        "POST",
                        "/api/internal/animego-push-sync",
                        headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
                        body=encoded_bundle,
                    )
                self.assertEqual(status, 502)
                self.assertEqual(json.loads(body)["status"], "partial")

                with patch.object(server, "MAX_ANIMEGO_PUSH_BODY_BYTES", 8):
                    status, _, _ = self.request_test_server(
                        db_path,
                        "POST",
                        "/api/internal/animego-push-sync",
                        headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
                        body=b'{"too":"large"}',
                    )
                self.assertEqual(status, 413)

    def test_animego_listing_http_500_is_counted_without_aborting_daily_sync(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            scrape_animego.init_db(db_path).close()
            args = sync_videos.parse_args(
                [
                    "--db",
                    db_path,
                    "--mode",
                    "daily",
                    "--source",
                    "animego",
                    "--animego-discover-pages",
                    "1",
                    "--animego-limit",
                    "0",
                    "--animego-missing-limit",
                    "0",
                    "--retry-attempts",
                    "1",
                ]
            )
            con = sync_videos.connect(db_path)
            try:
                error = urllib.error.HTTPError(args.animego_start_url, 500, "Internal Server Error", None, None)
                with patch.object(scrape_animego, "fetch_text", side_effect=error):
                    stats = sync_videos.sync_animego(con, args)
            finally:
                con.close()

        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["listing_failed"], 1)

    def test_next_daily_sync_run_uses_configured_utc_time(self):
        before_cutoff = dt.datetime(2026, 7, 8, 1, 59, tzinfo=dt.timezone.utc)
        after_cutoff = dt.datetime(2026, 7, 8, 20, 0, tzinfo=dt.timezone.utc)

        self.assertEqual(
            server.next_daily_sync_run(before_cutoff, hour=2, minute=0),
            dt.datetime(2026, 7, 8, 2, 0, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(
            server.next_daily_sync_run(after_cutoff, hour=2, minute=0),
            dt.datetime(2026, 7, 9, 2, 0, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(
            server.previous_daily_sync_run(before_cutoff, hour=2, minute=0),
            dt.datetime(2026, 7, 7, 2, 0, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(
            server.previous_daily_sync_run(after_cutoff, hour=2, minute=0),
            dt.datetime(2026, 7, 8, 2, 0, tzinfo=dt.timezone.utc),
        )

    def test_daily_sync_due_uses_each_enabled_source_last_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "animego.sqlite"
            scrape_animego.init_db(db_path).close()
            scheduled_at = dt.datetime(2026, 7, 10, 2, 0, tzinfo=dt.timezone.utc)

            self.assertTrue(server.content_sync_is_due(db_path, "daily", ["yummyanime"], scheduled_at))

            con = sync_videos.connect(db_path)
            try:
                sync_videos.ensure_sync_tables(con)
                con.executemany(
                    "insert into video_sync_state(key, value, updated_at) values (?, ?, ?)",
                    [
                        (
                            "yummyanime:daily:last_success",
                            "2026-07-10T02:05:00+00:00",
                            "2026-07-10T02:05:00+00:00",
                        ),
                        (
                            "animego:daily:last_success",
                            "2026-07-09T02:05:00+00:00",
                            "2026-07-09T02:05:00+00:00",
                        ),
                    ],
                )
                con.commit()
            finally:
                con.close()

            self.assertFalse(server.content_sync_is_due(db_path, "daily", ["yummyanime"], scheduled_at))
            self.assertTrue(
                server.content_sync_is_due(db_path, "daily", ["yummyanime", "animego"], scheduled_at)
            )

    def test_server_startup_marks_interrupted_content_runs_failed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "animego.sqlite"
            server.prepare_database(db_path)
            con = server.connect(db_path)
            try:
                run_id = content_updates.create_run(
                    con,
                    "daily",
                    "internal-daily-scheduler",
                    ["yummyanime"],
                    started_at="2026-07-10T02:00:00+00:00",
                )
                con.execute(
                    "update content_update_runs set stats_json = ? where id = ?",
                    ('{"preserved": true}', run_id),
                )
                con.commit()
            finally:
                con.close()

            self.assertEqual(server.recover_interrupted_content_sync_runs(db_path), 1)

            con = server.connect(db_path)
            try:
                row = con.execute(
                    "select status, error, stats_json, finished_at from content_update_runs where id = ?",
                    (run_id,),
                ).fetchone()
            finally:
                con.close()

        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"], "interrupted before server restart")
        self.assertEqual(row["stats_json"], '{"preserved": true}')
        self.assertIsNotNone(row["finished_at"])

    def test_video_sync_filters_empty_episodes(self):
        episodes = [{"number": "1"}, {"number": "2"}, {"number": "3"}]
        provider = {
            "episode_number": "2",
            "provider_id": "kodik-2",
            "translation_id": 1,
            "embed_url": "//kodikplayer.com/seria/2/token",
            "embed_url_redacted": "//kodikplayer.com/seria/2/<redacted>",
        }

        selected = sync_videos.filter_episodes_with_providers(episodes, [provider])
        self.assertEqual([episode["number"] for episode in selected], ["2"])

        global_provider = dict(provider, episode_number=None)
        selected = sync_videos.filter_episodes_with_providers(episodes, [global_provider])
        self.assertEqual(selected, episodes)

    def test_video_sync_manual_yummy_ref_infers_source(self):
        args = sync_videos.parse_args(
            [
                "--mode",
                "manual",
                "--yummy-ref",
                "https://ru.yummyani.me/catalog/item/vanpanchmen-2",
            ]
        )

        self.assertEqual(args.sources, ["yummyanime"])
        self.assertEqual(args.yummy_refs, ["https://ru.yummyani.me/catalog/item/vanpanchmen-2"])

    def test_video_sync_manual_catalog_years_infer_sources(self):
        args = sync_videos.parse_args(
            [
                "--mode",
                "manual",
                "--yummy-catalog-year",
                "2020",
                "--animego-season-year",
                "2019",
            ]
        )

        self.assertEqual(args.sources, ["yummyanime", "animego"])
        self.assertEqual(args.yummy_catalog_years, [2020])
        self.assertEqual(args.animego_season_years, [2019])

    def test_video_sync_manual_mode_requires_refs(self):
        with self.assertRaises(SystemExit):
            sync_videos.parse_args(["--mode", "manual"])

    def test_video_sync_legacy_yummy_ref_uses_legacy_parser(self):
        args = sync_videos.parse_args(
            [
                "--mode",
                "manual",
                "--yummy-ref",
                "https://yummyanime.tv/4085-belaja-zmeja-proishozhdenie.html",
            ]
        )

        with patch.object(scrape_yummyanime, "fetch_text", return_value="<html></html>") as fetch_text:
            with patch.object(
                scrape_yummyanime,
                "parse_detail",
                return_value=({"id": 1}, {}, [], []),
            ) as parse_detail:
                with patch.object(scrape_yummyanime, "parse_modern_detail") as parse_modern_detail:
                    sync_videos.parse_yummy_detail_for_sync(args.yummy_refs[0], args)

        fetch_text.assert_called_once_with(args.yummy_refs[0], delay=args.delay)
        parse_detail.assert_called_once()
        parse_modern_detail.assert_not_called()

    def test_video_sync_builds_animego_manual_item_from_url(self):
        item = sync_videos.animego_item_from_ref("https://animego.me/anime/vanpanchmen-3-2854")

        self.assertEqual(item["id"], 2854)
        self.assertEqual(item["slug"], "vanpanchmen-3-2854")
        self.assertEqual(item["source"], "animego")
        self.assertEqual(item["source_id"], "2854")

    def test_video_sync_detects_known_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            con = sync_videos.connect(db_path)
            try:
                scraped_at = server.now_iso()
                con.execute(
                    """
                    insert into anime(id, slug, title, url, source, source_id, scraped_at)
                    values (1, 'known', 'Known', 'https://animego.me/anime/known-1', 'animego', '1', ?)
                    """,
                    (scraped_at,),
                )
                con.execute(
                    """
                    insert into episodes(id, anime_id, number, has_video, scraped_at)
                    values (101, 1, '1', 1, ?)
                    """,
                    (scraped_at,),
                )
                con.execute(
                    """
                    insert into video_sources(
                        anime_id, episode_id, provider_id, translation_id,
                        embed_url, embed_url_redacted, scraped_at
                    )
                    values (1, 101, 'kodik', 7, '//kodik.test/source', '//kodik.test/<redacted>', ?)
                    """,
                    (scraped_at,),
                )

                self.assertTrue(
                    sync_videos.provider_known(
                        con,
                        101,
                        {
                            "provider_id": "kodik",
                            "translation_id": 7,
                            "embed_url_redacted": "//kodik.test/<redacted>",
                        },
                    )
                )
                self.assertFalse(
                    sync_videos.provider_known(
                        con,
                        101,
                        {
                            "provider_id": "kodik",
                            "translation_id": 8,
                            "embed_url_redacted": "//kodik.test/<redacted>",
                        },
                    )
                )
            finally:
                con.close()

    def test_video_sync_dry_run_savepoint_rolls_back_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            con = sync_videos.connect(db_path)
            try:
                args = type("Args", (), {"dry_run": True})()
                sync_videos.begin_title(con, args)
                con.execute(
                    """
                    insert into anime(id, slug, title, url, source, source_id, scraped_at)
                    values (1, 'dry-run', 'Dry Run', 'https://animego.me/anime/dry-run-1', 'animego', '1', ?)
                    """,
                    (server.now_iso(),),
                )
                sync_videos.finish_title(con, args)
                row = con.execute("select count(*) from anime where id = 1").fetchone()
                self.assertEqual(row[0], 0)
            finally:
                con.close()

    def test_prod_incremental_update_uses_railway_ssh_without_remote_backup(self):
        path = Path("scripts/prod_incremental_update.py")
        spec = importlib.util.spec_from_file_location("prod_incremental_update", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        args = module.parse_args(
            [
                "--yummy-ref",
                "https://ru.yummyani.me/catalog/item/vanpanchmen-2",
                "--print-command",
            ]
        )
        command = module.build_remote_command(args)

        self.assertIn("python3 sync_videos.py", command)
        self.assertIn("--mode manual", command)
        self.assertIn("--wait-lock", command)
        self.assertNotIn("animego-pre-incremental-", command)
        self.assertNotIn("BACKUP_DIR", command)
        self.assertNotIn("/data/backups", command)
        self.assertIn("integrity_check", command)
        self.assertNotIn("volume files upload", command)

    def test_yummy_modern_provider_skips_alloha(self):
        self.assertEqual(scrape_yummyanime.modern_provider_title("Плеер Alloha"), "Alloha")
        self.assertTrue(scrape_yummyanime.should_skip_modern_provider("Alloha"))
        self.assertFalse(scrape_yummyanime.should_skip_modern_provider("Kodik"))

    def test_yummy_modern_ids_do_not_collide_with_legacy_ids(self):
        self.assertEqual(scrape_yummyanime.internal_anime_id(4981), 10004981)
        self.assertEqual(scrape_yummyanime.internal_modern_anime_id(4981), 20004981)
        self.assertEqual(scrape_yummyanime.modern_source_id(4981), "yummyani:4981")
        self.assertNotEqual(
            scrape_yummyanime.internal_anime_id(4981),
            scrape_yummyanime.internal_modern_anime_id(4981),
        )

    def test_modern_yummyani_source_id_is_namespaced(self):
        anime = {
            "anime_id": 15,
            "anime_url": "neobyatnyy-okean-3",
            "title": "Необъятный океан 3",
            "year": 2026,
            "episodes": {"count": 1},
            "type": {"name": "TV"},
            "videos": [],
        }
        with patch.object(scrape_yummyanime, "fetch_modern_anime", return_value=(anime, "https://api.test/anime/15")):
            item, detail, episodes, providers = scrape_yummyanime.parse_modern_detail(
                "https://ru.yummyani.me/catalog/item/neobyatnyy-okean-3",
                include_embed_urls=False,
            )

        self.assertEqual(item["id"], 20000015)
        self.assertEqual(item["source"], "yummyanime")
        self.assertEqual(item["source_id"], "yummyani:15")
        self.assertEqual(detail["schema_data"]["source"], "yummyani")
        self.assertEqual(len(episodes), 1)
        self.assertEqual(providers, [])

    def test_source_sort_pins_dream_cast_above_popular_translations(self):
        sources = [
            self.source_row("AniStar", row_id=1),
            self.source_row("Dreamcast", row_id=2),
            self.source_row("AniDUB", row_id=3),
        ]
        rankings = {
            "anistar": {"rank": 0},
            "anidub": {"rank": 1},
            "dream cast": {"rank": 20},
        }
        context = server.build_source_ranking_context({1: sources}, rankings)

        sorted_sources = sorted(sources, key=lambda source: server.source_row_sort_key(source, context))

        self.assertEqual(sorted_sources[0]["translation_title"], "Dreamcast")

    def test_source_sort_demotes_subtitles_and_generic_labels(self):
        sources = [
            self.source_row("YummyAnime", row_id=1, source="yummyanime"),
            self.source_row("Субтитры", row_id=2),
            self.source_row("AnimeVost", row_id=3),
        ]
        rankings = {
            "субтитры": {"rank": 0},
            "animevost": {"rank": 5},
        }
        context = server.build_source_ranking_context({1: sources}, rankings)

        sorted_sources = sorted(sources, key=lambda source: server.source_row_sort_key(source, context))

        self.assertEqual([source["translation_title"] for source in sorted_sources], ["AnimeVost", "Субтитры", "YummyAnime"])

    def test_source_sort_prefers_title_wide_translation_coverage(self):
        episode_one = [
            self.source_row("AniStar", episode_id=1, row_id=1),
            self.source_row("SHIZA Project", episode_id=1, row_id=2),
        ]
        episode_two = [
            self.source_row("SHIZA Project", episode_id=2, row_id=3),
        ]
        rankings = {
            "anistar": {"rank": 0},
            "shiza project": {"rank": 10},
        }
        context = server.build_source_ranking_context({1: episode_one, 2: episode_two}, rankings)

        sorted_sources = sorted(episode_one, key=lambda source: server.source_row_sort_key(source, context))

        self.assertEqual(sorted_sources[0]["translation_title"], "SHIZA Project")

    def test_translation_rankings_exclude_generic_source_labels(self):
        con = server.connect()
        try:
            rankings = server.build_translation_rankings(con)
        finally:
            con.close()

        self.assertIn("dream cast", rankings)
        self.assertNotIn("yummyanime", rankings)

    def test_year_metadata_fields_are_available(self):
        items = server.get_anime_list()
        for year in ("2025", "2026"):
            with self.subTest(year=year):
                self.assertGreaterEqual(
                    sum(1 for item in items if item.get("year") == year or str(item.get("date_published") or "").startswith(year)),
                    10,
                )
        scored = [item for item in items if item.get("aggregate_score")]
        self.assertGreaterEqual(len(scored), 20)
        detail = server.get_anime_detail(scored[0]["id"])
        labels = {field["label"] for field in detail["fields"]}
        self.assertIn("Тип", labels)
        self.assertIn("Статус", labels)

    def test_effective_rating_prefers_external_rating_over_tiny_local_vote_count(self):
        items = server.get_anime_list()
        wixoss = next(item for item in items if item["id"] == 10005287)

        self.assertEqual(wixoss["aggregate_score"], 10.0)
        self.assertEqual(wixoss["aggregate_count"], 2)
        self.assertAlmostEqual(wixoss["external_score"], 5.6)
        self.assertEqual(wixoss["external_score_source"], "Shikimori")
        self.assertAlmostEqual(wixoss["effective_score"], 5.6)

        ranked = sorted(items, key=lambda item: (-(server.best_score(item) or 0), item["title"]))
        self.assertNotEqual(ranked[0]["id"], wixoss["id"])

    def test_user_state_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)
            user_id = self.create_google_user(db_path, "google-user-1", "one@example.com")

            anime_id = server.get_anime_list(db_path, user_id=user_id)[0]["id"]
            saved = server.update_user_state(
                anime_id,
                {"is_favorite": True, "progress_episode_number": 7, "watched": False},
                db_path,
                user_id,
            )
            self.assertTrue(saved["is_favorite"])
            self.assertEqual(saved["progress_episode_number"], 7)
            self.assertFalse(saved["watched"])

            item = next(item for item in server.get_anime_list(db_path, user_id=user_id) if item["id"] == anime_id)
            self.assertTrue(item["is_favorite"])
            self.assertEqual(item["progress_episode_number"], 7)
            self.assertFalse(item["watched"])

            server.update_user_state(anime_id, {"watched": True}, db_path, user_id)
            detail = server.get_anime_detail(anime_id, db_path, user_id)
            self.assertTrue(detail["is_favorite"])
            self.assertEqual(detail["progress_episode_number"], 7)
            self.assertTrue(detail["watched"])

    def test_user_activity_does_not_rebuild_catalog_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            anime_id = self.seed_watchable_title(db_path, anime_id=14, episode_count=1)
            user_id = self.create_google_user(db_path, "google-user-1", "one@example.com")
            detail = server.get_anime_detail(anime_id, db_path, user_id)

            server.invalidate_catalog_cache(db_path)
            with patch.object(server, "build_catalog_cache", wraps=server.build_catalog_cache) as build_cache:
                server.get_anime_detail(anime_id, db_path, user_id)
                self.assertEqual(build_cache.call_count, 1)

                server.update_user_state(anime_id, {"is_favorite": True}, db_path, user_id)
                server.record_watch_event(self.watch_payload(detail), db_path, user_id)
                os.utime(db_path, None)

                updated = server.get_anime_detail(anime_id, db_path, user_id)
                self.assertTrue(updated["is_favorite"])
                self.assertEqual(updated["progress_episode_number"], 1)
                self.assertEqual(build_cache.call_count, 1)

    def test_catalog_cache_detects_same_shape_external_update(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            anime_id = self.seed_watchable_title(
                db_path,
                anime_id=140,
                title="Old Title",
            )
            self.assertEqual(server.get_anime_detail(anime_id, db_path)["title"], "Old Title")

            raw = sqlite3.connect(db_path)
            try:
                # Same byte length and unchanged timestamps defeated the old
                # aggregate pseudo-signature.
                raw.execute("update anime set title = 'New Title' where id = ?", (anime_id,))
                raw.commit()
            finally:
                raw.close()

            self.assertEqual(server.get_anime_detail(anime_id, db_path)["title"], "New Title")

    def test_catalog_cache_token_reuses_request_connection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            self.seed_watchable_title(db_path, anime_id=143)
            server.get_catalog_cache(db_path)
            con = server.connect(db_path)
            try:
                with patch.object(
                    server.sqlite3,
                    "connect",
                    side_effect=AssertionError("unexpected cache-token connection"),
                ):
                    cache = server.get_catalog_cache(db_path, connection=con)
            finally:
                con.close()
            self.assertEqual(len(cache["items"]), 1)

    def test_catalog_revision_migration_covers_runtime_trigger_contract(self):
        migration = (
            server.ROOT
            / "migrations"
            / "2026-07-09_zzz-catalog-cache-revision"
            / "00_create_catalog_cache_revision.sql"
        ).read_text(encoding="utf-8").lower()

        for table in server.CATALOG_REVISION_TABLES:
            for operation in ("insert", "update", "delete"):
                trigger_name = server.catalog_revision_trigger_name(table, operation)
                self.assertIn(f"create trigger if not exists {trigger_name}", migration)

    def test_catalog_rebuild_does_not_hold_global_cache_lock(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            self.seed_watchable_title(db_path, anime_id=141)
            server.invalidate_catalog_cache(db_path)
            started = threading.Event()
            release = threading.Event()
            errors = []
            original_build = server.build_catalog_cache

            def delayed_build(path):
                started.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("catalog build test timed out")
                return original_build(path)

            def load_catalog():
                try:
                    server.get_anime_list(db_path)
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            with patch.object(server, "build_catalog_cache", side_effect=delayed_build):
                thread = threading.Thread(target=load_catalog)
                thread.start()
                self.assertTrue(started.wait(timeout=5))
                acquired = server.CATALOG_CACHE_LOCK.acquire(timeout=0.25)
                if acquired:
                    server.CATALOG_CACHE_LOCK.release()
                release.set()
                thread.join(timeout=5)

            self.assertTrue(acquired)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

    def test_watch_progress_event_does_not_clear_explicit_watched_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            anime_id = self.seed_watchable_title(db_path, anime_id=142)
            user_id = self.create_google_user(db_path, "watch-preserve", "watch@example.com")
            server.update_user_state(anime_id, {"watched": True}, db_path, user_id)
            detail = server.get_anime_detail(anime_id, db_path, user_id)

            result = server.record_watch_event(self.watch_payload(detail), db_path, user_id)

            self.assertTrue(result["state"]["watched"])
            self.assertTrue(server.get_anime_detail(anime_id, db_path, user_id)["watched"])

    def test_user_state_update_requires_real_user(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)
            anime_id = server.get_anime_list(db_path)[0]["id"]

            with self.assertRaisesRegex(ValueError, "user_id is required"):
                server.update_user_state(anime_id, {"is_favorite": True}, db_path)
            with self.assertRaisesRegex(ValueError, "user_id does not exist"):
                server.update_user_state(anime_id, {"is_favorite": True}, db_path, 999999)

    def watch_payload(self, detail, episode_index=0, event_type="player_engaged", session_id="watch-test"):
        episode = detail["episodes"][episode_index]
        source = detail["sources_by_episode"][episode["id"]][0]
        return {
            "client_session_id": session_id,
            "event_type": event_type,
            "anime_id": detail["id"],
            "episode_id": episode["id"],
            "episode_number": episode["number"],
            "progress_episode_number": int(episode["number"]),
            "video_source_id": source["id"],
            "source": source["source"],
            "source_anime_id": source["source_anime_id"],
            "translation_id": source["translation_id"],
            "translation_title": source["translation_title"],
            "provider_id": source["provider_id"],
            "provider_title": source["provider_title"],
            "embed_host": source["embed_host"],
            "page_visible": True,
            "player_focused": True,
        }

    def test_watch_event_loaded_does_not_update_manual_progress(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            anime_id = self.seed_watchable_title(db_path, anime_id=10, episode_count=2)
            user_id = self.create_google_user(db_path, "google-user-1", "one@example.com")
            detail = server.get_anime_detail(anime_id, db_path, user_id)

            payload = self.watch_payload(detail, event_type="player_loaded")
            result = server.record_watch_event(payload, db_path, user_id)

            self.assertEqual(result["event"]["event_type"], "player_loaded")
            self.assertIsNone(result["state"]["progress_episode_number"])
            con = server.connect(db_path)
            try:
                state_row = con.execute("select * from user_episode_state where user_id = ?", (user_id,)).fetchone()
                self.assertIsNotNone(state_row)
                self.assertIsNone(state_row["started_at"])
                self.assertEqual(state_row["engaged_seconds"], 0)
            finally:
                con.close()

    def test_title_navigation_remembers_episode_without_starting_title(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            anime_id = self.seed_watchable_title(db_path, anime_id=101, episode_count=6)
            user_id = self.create_google_user(db_path, "navigation-user", "navigation@example.com")
            detail = server.get_anime_detail(anime_id, db_path, user_id)
            episode = detail["episodes"][5]

            self.assertNotIn("last_opened_episode", detail)
            saved = server.update_title_navigation(anime_id, episode["id"], db_path, user_id)

            self.assertEqual(saved["episode_id"], episode["id"])
            self.assertEqual(saved["episode_number"], "6")
            updated = server.get_anime_detail(anime_id, db_path, user_id)
            self.assertEqual(updated["last_opened_episode"]["episode_id"], episode["id"])
            self.assertEqual(updated["last_opened_episode"]["episode_number"], "6")
            self.assertIsNone(updated["progress_episode_number"])
            self.assertEqual(updated["watch_status"], "none")
            self.assertFalse(updated["watched"])

    def test_title_navigation_rejects_episode_from_another_title(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            first_id = self.seed_watchable_title(db_path, anime_id=102, episode_count=2)
            second_id = self.seed_watchable_title(db_path, anime_id=103, episode_count=1)
            user_id = self.create_google_user(db_path, "navigation-user", "navigation@example.com")
            other_episode_id = server.get_anime_detail(second_id, db_path, user_id)["episodes"][0]["id"]

            with self.assertRaisesRegex(ValueError, "invalid for this title"):
                server.update_title_navigation(first_id, other_episode_id, db_path, user_id)

    def test_title_navigation_api_persists_episode_without_progress(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            anime_id = self.seed_watchable_title(db_path, anime_id=105, episode_count=2)
            user_id = self.create_google_user(db_path, "navigation-api-user", "navigation-api@example.com")
            token = self.create_session(db_path, user_id)
            episode = server.get_anime_detail(anime_id, db_path, user_id)["episodes"][1]

            status, _, response = self.request_test_server(
                db_path,
                "PATCH",
                f"/api/anime/{anime_id}/navigation",
                headers={
                    "Cookie": f"{server.SESSION_COOKIE_NAME}={token}",
                    "Content-Type": "application/json",
                },
                body=json.dumps({"episode_id": episode["id"]}).encode("utf-8"),
            )

            self.assertEqual(status, 200)
            payload = json.loads(response)
            self.assertEqual(payload["navigation"]["episode_id"], episode["id"])
            detail = server.get_anime_detail(anime_id, db_path, user_id)
            self.assertEqual(detail["last_opened_episode"]["episode_id"], episode["id"])
            self.assertIsNone(detail["progress_episode_number"])
            self.assertEqual(detail["watch_status"], "none")

    def test_episode_and_source_selection_do_not_update_watch_progress(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            anime_id = self.seed_watchable_title(db_path, anime_id=104, episode_count=2)
            user_id = self.create_google_user(db_path, "navigation-user", "navigation@example.com")
            detail = server.get_anime_detail(anime_id, db_path, user_id)

            for index, event_type in enumerate(("episode_selected", "source_changed")):
                result = server.record_watch_event(
                    self.watch_payload(
                        detail,
                        episode_index=1,
                        event_type=event_type,
                        session_id=f"navigation-{index}",
                    ),
                    db_path,
                    user_id,
                )
                self.assertIsNone(result["state"]["progress_episode_number"])
                self.assertEqual(result["state"]["watch_status"], "none")
                self.assertIsNone(result["episode_state"]["started_at"])

            updated = server.get_anime_detail(anime_id, db_path, user_id)
            self.assertIsNone(updated["progress_episode_number"])
            self.assertEqual(updated["watch_status"], "none")

    def test_watch_engagement_updates_title_progress_and_continue_resume(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            anime_id = self.seed_watchable_title(db_path, anime_id=11, episode_count=2)
            user_id = self.create_google_user(db_path, "google-user-1", "one@example.com")
            detail = server.get_anime_detail(anime_id, db_path, user_id)

            result = server.record_watch_event(self.watch_payload(detail), db_path, user_id)

            self.assertEqual(result["state"]["progress_episode_number"], 1)
            self.assertFalse(result["state"]["watched"])
            updated = server.get_anime_detail(anime_id, db_path, user_id)
            self.assertEqual(updated["progress_episode_number"], 1)

            target = server.get_continue_watching(db_path, user_id)["item"]
            self.assertEqual(target["anime_id"], anime_id)
            self.assertEqual(target["episode_number"], "1")
            self.assertEqual(target["reason"], "resume")

    def test_detail_uses_last_watch_when_title_progress_is_stale(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            anime_id = self.seed_watchable_title(db_path, anime_id=14, episode_count=2)
            user_id = self.create_google_user(db_path, "google-user-1", "one@example.com")
            detail = server.get_anime_detail(anime_id, db_path, user_id)
            server.record_watch_event(self.watch_payload(detail, episode_index=1), db_path, user_id)
            con = server.connect(db_path)
            try:
                con.execute(
                    """
                    update user_title_state
                    set progress_episode_number = 1,
                        updated_at = ?
                    where user_id = ?
                      and anime_id = ?
                    """,
                    (server.now_iso(), user_id, anime_id),
                )
                con.commit()
            finally:
                con.close()

            updated = server.get_anime_detail(anime_id, db_path, user_id)

            self.assertEqual(updated["progress_episode_number"], 2)
            self.assertEqual(updated["last_watch"]["episode_number"], "2")
            self.assertEqual(updated["last_watch"]["progress_episode_number"], 2)
            self.assertEqual(
                updated["last_watch"]["video_source_id"],
                updated["sources_by_episode"][updated["last_watch"]["episode_id"]][0]["id"],
            )

    def test_manual_progress_update_moves_last_watch_episode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            anime_id = self.seed_watchable_title(db_path, anime_id=15, episode_count=2)
            user_id = self.create_google_user(db_path, "google-user-1", "one@example.com")
            detail = server.get_anime_detail(anime_id, db_path, user_id)
            server.record_watch_event(self.watch_payload(detail, episode_index=1), db_path, user_id)

            saved = server.update_user_state(anime_id, {"progress_episode_number": 1}, db_path, user_id)

            self.assertEqual(saved["progress_episode_number"], 1)
            self.assertEqual(saved["last_watch"]["episode_number"], "1")
            self.assertEqual(saved["last_watch"]["progress_episode_number"], 1)
            updated = server.get_anime_detail(anime_id, db_path, user_id)
            self.assertEqual(updated["progress_episode_number"], 1)
            self.assertEqual(updated["last_watch"]["episode_number"], "1")
            con = server.connect(db_path)
            try:
                manual_row = con.execute(
                    """
                    select *
                    from user_episode_state
                    where user_id = ?
                      and anime_id = ?
                      and progress_episode_number = 1
                    """,
                    (user_id, anime_id),
                ).fetchone()
                self.assertIsNotNone(manual_row)
                self.assertEqual(manual_row["last_event_type"], "manual_progress")
                self.assertIsNotNone(manual_row["started_at"])
            finally:
                con.close()

    def test_manual_progress_clear_removes_title_from_continue_watching(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            anime_id = self.seed_watchable_title(db_path, anime_id=16, episode_count=2)
            user_id = self.create_google_user(db_path, "google-user-1", "one@example.com")
            detail = server.get_anime_detail(anime_id, db_path, user_id)
            server.record_watch_event(self.watch_payload(detail), db_path, user_id)

            saved = server.update_user_state(
                anime_id,
                {"progress_episode_number": None, "watched": False},
                db_path,
                user_id,
            )

            self.assertIsNone(saved["progress_episode_number"])
            self.assertIsNone(saved["last_watch"])
            updated = server.get_anime_detail(anime_id, db_path, user_id)
            self.assertIsNone(updated["progress_episode_number"])
            self.assertNotIn("last_watch", updated)
            self.assertIsNone(server.get_continue_watching(db_path, user_id)["item"])
            con = server.connect(db_path)
            try:
                cleared_row = con.execute(
                    """
                    select *
                    from user_episode_state
                    where user_id = ?
                      and anime_id = ?
                    """,
                    (user_id, anime_id),
                ).fetchone()
                self.assertIsNotNone(cleared_row)
                self.assertEqual(cleared_row["last_event_type"], "manual_clear")
                self.assertIsNone(cleared_row["started_at"])
            finally:
                con.close()

    def test_continue_watching_opens_next_episode_after_likely_completion(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            anime_id = self.seed_watchable_title(db_path, anime_id=12, episode_count=2)
            user_id = self.create_google_user(db_path, "google-user-1", "one@example.com")
            detail = server.get_anime_detail(anime_id, db_path, user_id)

            with patch.object(server, "WATCH_LIKELY_COMPLETED_SECONDS", 60):
                server.record_watch_event(self.watch_payload(detail), db_path, user_id)
                heartbeat = self.watch_payload(detail, event_type="heartbeat")
                heartbeat["engaged_seconds"] = 75
                server.record_watch_event(heartbeat, db_path, user_id)

            target = server.get_continue_watching(db_path, user_id)["item"]
            self.assertEqual(target["anime_id"], anime_id)
            self.assertEqual(target["episode_number"], "2")
            self.assertEqual(target["reason"], "next_episode")

    def test_watch_event_api_requires_auth_and_records_progress(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            anime_id = self.seed_watchable_title(db_path, anime_id=13, episode_count=1)
            user_id = self.create_google_user(db_path, "google-user-1", "one@example.com")
            token = self.create_session(db_path, user_id)
            detail = server.get_anime_detail(anime_id, db_path, user_id)
            body = json.dumps(self.watch_payload(detail)).encode("utf-8")

            status, _, response = self.request_test_server(
                db_path,
                "POST",
                "/api/watch-events",
                headers={
                    "Cookie": f"{server.SESSION_COOKIE_NAME}={token}",
                    "Content-Type": "application/json",
                },
                body=body,
            )

            self.assertEqual(status, 200)
            payload = json.loads(response)
            self.assertEqual(payload["state"]["progress_episode_number"], 1)

    def test_recommendation_profile_ignores_short_watch_and_weights_explicit_intent_higher(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            short_id = self.seed_watchable_title(db_path, anime_id=20, title="Short Watch", episode_count=1)
            meaningful_id = self.seed_watchable_title(db_path, anime_id=21, title="Meaningful Watch", episode_count=1)
            favorite_id = self.seed_watchable_title(db_path, anime_id=22, title="Favorite Seed", episode_count=1)
            watched_id = self.seed_watchable_title(db_path, anime_id=23, title="Watched Seed", episode_count=1)
            user_id = self.create_google_user(db_path, "google-user-1", "one@example.com")
            con = server.connect(db_path)
            try:
                con.executemany(
                    "insert into anime_genres(anime_id, genre) values (?, ?)",
                    [
                        (short_id, "Short Genre"),
                        (meaningful_id, "Meaningful Genre"),
                        (favorite_id, "Favorite Genre"),
                        (watched_id, "Watched Genre"),
                    ],
                )
                con.commit()
            finally:
                con.close()

            short_detail = server.get_anime_detail(short_id, db_path, user_id)
            server.record_watch_event(self.watch_payload(short_detail), db_path, user_id)

            meaningful_detail = server.get_anime_detail(meaningful_id, db_path, user_id)
            server.record_watch_event(self.watch_payload(meaningful_detail), db_path, user_id)
            heartbeat = self.watch_payload(meaningful_detail, event_type="heartbeat")
            heartbeat["engaged_seconds"] = server.MEANINGFUL_WATCH_SECONDS
            server.record_watch_event(heartbeat, db_path, user_id)

            server.update_user_state(favorite_id, {"is_favorite": True}, db_path, user_id)
            server.update_user_state(watched_id, {"watched": True}, db_path, user_id)

            items = {item["id"]: item for item in server.get_anime_list(db_path, user_id=user_id)}
            self.assertEqual(items[short_id]["progress_episode_number"], 1)
            self.assertEqual(server.seed_weight(items[short_id]), 0.0)
            self.assertEqual(items[meaningful_id]["meaningful_watch_seconds"], server.MEANINGFUL_WATCH_SECONDS)
            self.assertEqual(server.seed_weight(items[meaningful_id]), server.MEANINGFUL_WATCH_RECOMMENDATION_SEED_WEIGHT)
            self.assertEqual(server.seed_weight(items[favorite_id]), server.FAVORITE_RECOMMENDATION_SEED_WEIGHT)
            self.assertEqual(server.seed_weight(items[watched_id]), server.WATCHED_RECOMMENDATION_SEED_WEIGHT)
            self.assertEqual(server.seed_weight(items[favorite_id]), 3.0)
            self.assertEqual(
                server.seed_weight(items[watched_id]),
                server.WATCHED_RECOMMENDATION_SEED_WEIGHT,
            )
            self.assertLess(server.seed_weight(items[watched_id]), server.seed_weight(items[favorite_id]))
            self.assertGreater(server.seed_weight(items[meaningful_id]), 0.0)
            self.assertLess(server.seed_weight(items[meaningful_id]), server.seed_weight(items[watched_id]))
            self.assertGreaterEqual(
                server.seed_weight(items[favorite_id]) / server.seed_weight(items[meaningful_id]),
                3.0,
            )

            profile = server.build_recommendation_profile(items.values())
            self.assertNotIn(server.normalize_key("Short Genre"), profile["genre_weights"])
            self.assertEqual(
                profile["genre_weights"][server.normalize_key("Meaningful Genre")],
                server.MEANINGFUL_WATCH_RECOMMENDATION_SEED_WEIGHT,
            )
            self.assertEqual(
                profile["genre_weights"][server.normalize_key("Favorite Genre")],
                server.FAVORITE_RECOMMENDATION_SEED_WEIGHT,
            )
            self.assertEqual(
                profile["genre_weights"][server.normalize_key("Watched Genre")],
                server.WATCHED_RECOMMENDATION_SEED_WEIGHT,
            )

    def test_api_requires_authenticated_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)

            status, _, _ = self.request_test_server(db_path, "GET", "/api/anime")
            self.assertEqual(status, 401)

            status, headers, _ = self.request_test_server(db_path, "GET", "/")
            self.assertEqual(status, 302)
            self.assertTrue(headers["Location"].startswith("/login?next="))

            user_id = self.create_google_user(db_path, "google-user-1", "one@example.com")
            token = self.create_session(db_path, user_id)
            status, _, body = self.request_test_server(
                db_path,
                "GET",
                "/api/me",
                headers={"Cookie": f"{server.SESSION_COOKIE_NAME}={token}"},
            )
            self.assertEqual(status, 200)
            self.assertIn(b"one@example.com", body)

    def test_api_accepts_session_cookie_alongside_google_g_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)
            user_id = self.create_google_user(db_path, "google-g-state", "one@example.com")
            token = self.create_session(db_path, user_id)
            g_state = 'g_state={"i_l":0,"i_ll":123}'

            for cookie_header in (
                f"{g_state}; {server.SESSION_COOKIE_NAME}={token}",
                f"{server.SESSION_COOKIE_NAME}={token}; {g_state}",
            ):
                with self.subTest(cookie_header=cookie_header):
                    status, _, body = self.request_test_server(
                        db_path,
                        "GET",
                        "/api/me",
                        headers={"Cookie": cookie_header},
                    )

                    self.assertEqual(status, 200)
                    self.assertIn(b"one@example.com", body)

    def test_api_tries_duplicate_session_cookie_candidates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)
            user_id = self.create_google_user(db_path, "duplicate-session", "one@example.com")
            token = self.create_session(db_path, user_id)

            for cookie_header in (
                f"{server.SESSION_COOKIE_NAME}=stale; {server.SESSION_COOKIE_NAME}={token}",
                f"{server.SESSION_COOKIE_NAME}={token}; {server.SESSION_COOKIE_NAME}=stale",
            ):
                with self.subTest(cookie_header=cookie_header):
                    status, _, body = self.request_test_server(
                        db_path,
                        "GET",
                        "/api/me",
                        headers={"Cookie": cookie_header},
                    )

                    self.assertEqual(status, 200)
                    self.assertIn(b"one@example.com", body)

    def test_logout_revokes_duplicate_session_cookie_candidates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)
            user_id = self.create_google_user(db_path, "duplicate-logout", "one@example.com")
            first_token = self.create_session(db_path, user_id)
            second_token = self.create_session(db_path, user_id)
            cookie_header = (
                f'g_state={{"i_l":0}}; {server.SESSION_COOKIE_NAME}={first_token}; '
                f"{server.SESSION_COOKIE_NAME}={second_token}"
            )

            status, _, body = self.request_test_server(
                db_path,
                "POST",
                "/api/logout",
                headers={"Cookie": cookie_header},
            )

            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"ok": True})
            self.assertIsNone(server.get_session_user(first_token, db_path))
            self.assertIsNone(server.get_session_user(second_token, db_path))

    def test_session_updates_last_seen_and_honors_current_allowlist(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            self.seed_watchable_title(db_path)
            user_id = self.create_google_user(db_path, "session-user", "session@example.com")
            token = self.create_session(db_path, user_id)
            token_hash = server.session_token_hash(token)
            con = server.connect(db_path)
            con.execute(
                "update sessions set last_seen_at = '2000-01-01T00:00:00+00:00' where token_hash = ?",
                (token_hash,),
            )
            con.commit()
            con.close()

            self.assertEqual(server.get_session_user(token, db_path)["email"], "session@example.com")
            con = server.connect(db_path)
            refreshed = con.execute(
                "select last_seen_at from sessions where token_hash = ?",
                (token_hash,),
            ).fetchone()[0]
            con.close()
            self.assertGreater(refreshed, "2000-01-01T00:00:00+00:00")

            with patch.dict(os.environ, {"ANIME_AUTH_ALLOWED_EMAILS": "other@example.com"}):
                self.assertIsNone(server.get_session_user(token, db_path))
            con = server.connect(db_path)
            revoked_at = con.execute(
                "select revoked_at from sessions where token_hash = ?",
                (token_hash,),
            ).fetchone()[0]
            con.close()
            self.assertIsNotNone(revoked_at)

    def test_catalog_api_defers_search_fields_to_dedicated_endpoint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            self.seed_watchable_title(db_path, anime_id=1, title="Как поживаете?")
            con = server.connect(db_path)
            try:
                timestamp = server.now_iso()
                con.execute(
                    """
                    insert into anime_title_aliases (
                        anime_id, alias, normalized_alias, language, alias_type,
                        source, created_at, updated_at
                    ) values (1, 'Мальчик и херон', ?, 'ru', 'manual', 'test', ?, ?)
                    """,
                    (server.normalize_search_text("Мальчик и херон"), timestamp, timestamp),
                )
                con.commit()
            finally:
                con.close()
            user_id = self.create_google_user(db_path, "google-user-1", "one@example.com")
            token = self.create_session(db_path, user_id)
            cookie = {"Cookie": f"{server.SESSION_COOKIE_NAME}={token}"}

            status, _, body = self.request_test_server(db_path, "GET", "/api/anime", headers=cookie)
            self.assertEqual(status, 200)
            catalog_items = json.loads(body)["items"]
            self.assertTrue(catalog_items)
            self.assertNotIn("search_fields", catalog_items[0])

            status, _, body = self.request_test_server(
                db_path,
                "GET",
                "/api/anime/search-fields",
                headers=cookie,
            )
            self.assertEqual(status, 200)
            search_items = json.loads(body)["items"]
            self.assertEqual(search_items[0]["id"], 1)
            self.assertEqual(search_items[0]["search_fields"][0]["value"], "Мальчик и херон")

    def test_admin_access_is_limited_to_configured_email(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)
            con = server.connect(db_path)
            try:
                con.execute("delete from sessions")
                con.execute("delete from user_title_state")
                con.execute("delete from users")
                con.commit()
            finally:
                con.close()

            admin_id = self.create_google_user(db_path, "google-user-1", "one@example.com")
            other_id = self.create_google_user(db_path, "google-user-2", "two@example.com")
            admin_token = self.create_session(db_path, admin_id)
            other_token = self.create_session(db_path, other_id)
            revoked_token = self.create_session(db_path, other_id)
            server.revoke_session(revoked_token, db_path)
            anime_id = server.get_anime_list(db_path, user_id=admin_id)[0]["id"]
            stats_anime_id = self.seed_watchable_title(
                db_path,
                anime_id=900001,
                title="Admin Stats Title",
                episode_count=6,
            )
            server.update_user_state(
                anime_id,
                {"is_favorite": True, "progress_episode_number": 4},
                db_path,
                admin_id,
            )
            server.update_user_state(anime_id, {"watched": True}, db_path, other_id)
            stats_detail = server.get_anime_detail(stats_anime_id, db_path, other_id)
            for episode_index in range(5):
                server.record_watch_event(
                    self.watch_payload(
                        stats_detail,
                        episode_index=episode_index,
                        session_id=f"admin-stats-{episode_index}",
                    ),
                    db_path,
                    other_id,
                )
            meaningful = self.watch_payload(
                stats_detail,
                episode_index=0,
                event_type="heartbeat",
                session_id="admin-stats-0",
            )
            meaningful["engaged_seconds"] = server.MEANINGFUL_WATCH_SECONDS
            server.record_watch_event(meaningful, db_path, other_id)
            server.record_watch_event(
                self.watch_payload(
                    stats_detail,
                    episode_index=5,
                    event_type="player_loaded",
                    session_id="admin-stats-weak-open",
                ),
                db_path,
                other_id,
            )
            server.update_user_state(
                stats_anime_id,
                {"watch_status": "none"},
                db_path,
                other_id,
            )

            with patch.dict(os.environ, {"ANIME_ADMIN_EMAIL": "one@example.com"}):
                status, _, body = self.request_test_server(
                    db_path,
                    "GET",
                    "/api/me",
                    headers={"Cookie": f"{server.SESSION_COOKIE_NAME}={admin_token}"},
                )
                self.assertEqual(status, 200)
                self.assertTrue(json.loads(body)["user"]["is_admin"])

                status, _, body = self.request_test_server(
                    db_path,
                    "GET",
                    "/api/me",
                    headers={"Cookie": f"{server.SESSION_COOKIE_NAME}={other_token}"},
                )
                self.assertEqual(status, 200)
                self.assertFalse(json.loads(body)["user"]["is_admin"])

                status, _, body = self.request_test_server(
                    db_path,
                    "GET",
                    "/admin",
                    headers={"Cookie": f"{server.SESSION_COOKIE_NAME}={admin_token}"},
                )
                self.assertEqual(status, 200)
                self.assertIn(b"/static/admin.js", body)

                status, _, body = self.request_test_server(
                    db_path,
                    "GET",
                    "/static/admin.js",
                    headers={"Cookie": f"{server.SESSION_COOKIE_NAME}={admin_token}"},
                )
                self.assertEqual(status, 200)
                self.assertIn(b"/api/admin/users", body)

                status, _, _ = self.request_test_server(
                    db_path,
                    "GET",
                    "/admin",
                    headers={"Cookie": f"{server.SESSION_COOKIE_NAME}={other_token}"},
                )
                self.assertEqual(status, 404)

                status, _, _ = self.request_test_server(
                    db_path,
                    "GET",
                    "/static/admin.js",
                    headers={"Cookie": f"{server.SESSION_COOKIE_NAME}={other_token}"},
                )
                self.assertEqual(status, 404)

                status, _, _ = self.request_test_server(
                    db_path,
                    "GET",
                    "/api/admin/users",
                    headers={"Cookie": f"{server.SESSION_COOKIE_NAME}={other_token}"},
                )
                self.assertEqual(status, 404)

                status, _, body = self.request_test_server(
                    db_path,
                    "GET",
                    "/api/admin/users",
                    headers={"Cookie": f"{server.SESSION_COOKIE_NAME}={admin_token}"},
                )
                self.assertEqual(status, 200)

            payload = json.loads(body)
            self.assertEqual(payload["summary"]["registered_users"], 2)
            self.assertEqual(payload["summary"]["total_favorites"], 1)
            self.assertEqual(payload["summary"]["total_progress_titles"], 1)
            self.assertEqual(payload["summary"]["total_watched_titles"], 1)
            self.assertEqual(payload["summary"]["total_started_episodes"], 5)
            self.assertEqual(payload["summary"]["total_meaningful_episodes"], 1)
            self.assertEqual(payload["summary"]["valid_authorizations"], 2)
            self.assertEqual(payload["summary"]["active_users_7d"], 2)
            self.assertEqual(payload["summary"]["logins_7d"], 3)
            users = {item["email"]: item for item in payload["users"]}
            self.assertEqual(set(users), {"one@example.com", "two@example.com"})
            self.assertTrue(users["one@example.com"]["is_admin"])
            self.assertFalse(users["two@example.com"]["is_admin"])
            self.assertEqual(users["one@example.com"]["favorite_titles"], 1)
            self.assertEqual(users["two@example.com"]["watched_titles"], 1)
            self.assertEqual(users["two@example.com"]["completed_titles"], 1)
            self.assertEqual(users["two@example.com"]["started_episodes"], 5)
            self.assertEqual(users["two@example.com"]["opened_episodes"], 6)
            self.assertEqual(users["two@example.com"]["meaningful_episodes"], 1)
            self.assertEqual(users["two@example.com"]["login_count"], 2)
            self.assertEqual(users["two@example.com"]["valid_authorizations"], 1)
            self.assertEqual(users["two@example.com"]["last_watch_title"], "Admin Stats Title")
            self.assertTrue(users["two@example.com"]["viewer_7d"])
            self.assertTrue(payload["top_titles"])
            stats_title = next(
                item for item in payload["top_titles"] if item["anime_id"] == stats_anime_id
            )
            self.assertEqual(stats_title["started_episodes"], 5)
            self.assertEqual(stats_title["meaningful_episodes"], 1)
            self.assertTrue(payload["recent_watch_sessions"])
            self.assertIsNotNone(payload["telemetry_started_at"])

    def test_admin_analytics_canonicalize_variants_and_only_sessions_are_logins(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)
            con = server.connect(db_path)
            try:
                con.execute("delete from sessions")
                con.execute("delete from user_title_state")
                con.execute("delete from users")
                con.commit()
            finally:
                con.close()

            merged = next(
                item
                for item in server.get_anime_list(db_path)
                if len(item.get("source_variants") or []) > 1
                and item.get("available_episode_count", 0) > 0
            )
            canonical_id = int(merged["id"])
            variant_id = next(
                int(variant["id"])
                for variant in merged["source_variants"]
                if int(variant["id"]) != canonical_id
            )
            admin_id = self.create_google_user(
                db_path,
                "canonical-admin",
                "canonical-admin@example.com",
            )
            self.create_google_user(
                db_path,
                "never-logged-in",
                "never@example.com",
            )
            self.create_session(db_path, admin_id)

            con = server.connect(db_path)
            try:
                con.executemany(
                    """
                    insert into user_title_state (
                        user_id, anime_id, is_favorite, progress_episode_number,
                        watched, updated_at, watch_status, not_interested,
                        favorite_updated_at, watch_status_updated_at
                    ) values (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        (
                            admin_id,
                            canonical_id,
                            1,
                            1,
                            1,
                            "2026-07-13T10:00:00+00:00",
                            "completed",
                            "2026-07-13T10:00:00+00:00",
                            "2026-07-13T10:00:00+00:00",
                        ),
                        (
                            admin_id,
                            variant_id,
                            0,
                            None,
                            0,
                            "2026-07-13T11:00:00+00:00",
                            "none",
                            "2026-07-13T11:00:00+00:00",
                            None,
                        ),
                    ),
                )
                con.commit()
            finally:
                con.close()

            detail = server.get_anime_detail(canonical_id, db_path, admin_id)
            server.record_watch_event(
                self.watch_payload(detail, session_id="canonical-event"),
                db_path,
                admin_id,
            )
            server.record_watch_event(
                self.watch_payload(detail, session_id="variant-event"),
                db_path,
                admin_id,
            )
            con = server.connect(db_path)
            try:
                con.execute(
                    """
                    update user_watch_events
                    set anime_id = ?
                    where user_id = ? and client_session_id = 'variant-event'
                    """,
                    (variant_id, admin_id),
                )
                con.commit()
            finally:
                con.close()

            con = server.connect(db_path)
            try:
                server.prepare_admin_canonical_context(con, db_path)
                self.assertFalse(con.in_transaction)
            finally:
                con.close()

            with patch.dict(
                os.environ,
                {"ANIME_ADMIN_EMAIL": "canonical-admin@example.com"},
            ):
                payload = server.admin_users_payload(db_path)

            self.assertEqual(payload["summary"]["registered_users"], 2)
            self.assertEqual(payload["summary"]["logins_7d"], 1)
            self.assertEqual(payload["summary"]["valid_authorizations"], 1)
            self.assertEqual(payload["summary"]["total_favorites"], 0)
            self.assertEqual(payload["summary"]["total_progress_titles"], 0)
            self.assertEqual(payload["summary"]["total_completed_titles"], 1)
            self.assertEqual(payload["summary"]["total_started_episodes"], 1)

            users = {item["email"]: item for item in payload["users"]}
            admin = users["canonical-admin@example.com"]
            never_logged_in = users["never@example.com"]
            self.assertEqual(admin["favorite_titles"], 0)
            self.assertEqual(admin["progress_titles"], 0)
            self.assertEqual(admin["completed_titles"], 1)
            self.assertEqual(admin["started_episodes"], 1)
            self.assertEqual(admin["episode_titles"], 1)
            self.assertEqual(admin["last_watch_anime_id"], canonical_id)
            self.assertEqual(never_logged_in["login_count"], 0)
            self.assertIsNone(never_logged_in["last_login_at"])
            self.assertFalse(never_logged_in["active_7d"])
            self.assertTrue(any(item["anime_id"] == canonical_id for item in payload["top_titles"]))
            self.assertFalse(any(item["anime_id"] == variant_id for item in payload["top_titles"]))
            self.assertTrue(payload["recent_watch_sessions"])
            self.assertTrue(
                all(
                    item["anime_id"] == canonical_id
                    for item in payload["recent_watch_sessions"]
                )
            )

    def test_admin_route_redirects_anonymous_user_to_login(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)

            status, headers, _ = self.request_test_server(db_path, "GET", "/admin")

            self.assertEqual(status, 302)
            self.assertEqual(urlparse(headers["Location"]).path, "/login")

    def test_oversized_auth_payload_is_rejected_before_parsing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            server.prepare_database(db_path)

            status, _, body = self.request_test_server(
                db_path,
                "POST",
                "/api/auth/google",
                headers={"Content-Type": "application/json"},
                body="x" * (server.MAX_JSON_BODY_BYTES + 1),
            )

            self.assertEqual(status, 413)
            self.assertEqual(json.loads(body)["error"], "payload too large")

    def test_oversized_state_patch_is_rejected_before_parsing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            anime_id = self.seed_watchable_title(db_path)
            user_id = self.create_google_user(db_path, "google-user-1", "one@example.com")
            token = self.create_session(db_path, user_id)

            status, _, body = self.request_test_server(
                db_path,
                "PATCH",
                f"/api/anime/{anime_id}/state",
                headers={
                    "Content-Type": "application/json",
                    "Cookie": f"{server.SESSION_COOKIE_NAME}={token}",
                },
                body="x" * (server.MAX_JSON_BODY_BYTES + 1),
            )

            self.assertEqual(status, 413)
            self.assertEqual(json.loads(body)["error"], "payload too large")

    def test_client_error_endpoint_logs_without_auth_and_redacts_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            log_dir = f"{tmpdir}/logs"
            shutil.copy(server.DEFAULT_DB, db_path)
            payload = {
                "type": "TypeError",
                "message": "Boom token=abc https://kodikplayer.com/embed/private",
                "stack": "Error: Boom\ncredential=secret",
                "url": "/login?next=/",
                "context": {
                    "action": "boot login",
                    "credential": "secret",
                    "nested": {"cookie": "session=secret"},
                },
            }

            with patch.dict(os.environ, {"ANIME_LOG_DIR": log_dir}):
                status, _, body = self.request_test_server(
                    db_path,
                    "POST",
                    "/api/client-errors",
                    headers={"Content-Type": "application/json"},
                    body=json.dumps(payload),
                )

            self.assertEqual(status, 202)
            self.assertEqual(json.loads(body), {"ok": True})
            event = self.latest_json_log_entry(log_dir, "client-errors.log")
            self.assertFalse(event["authenticated"])
            self.assertEqual(event["type"], "TypeError")
            self.assertIn("token=<redacted>", event["message"])
            self.assertIn("<redacted-url>", event["message"])
            self.assertIn("credential=<redacted>", event["stack"])
            self.assertEqual(event["context"]["credential"], "<redacted>")
            self.assertEqual(event["context"]["nested"]["cookie"], "<redacted>")

    def test_client_error_endpoint_rejects_invalid_and_oversized_payloads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            log_dir = f"{tmpdir}/logs"
            shutil.copy(server.DEFAULT_DB, db_path)

            with patch.dict(os.environ, {"ANIME_LOG_DIR": log_dir}):
                status, _, body = self.request_test_server(
                    db_path,
                    "POST",
                    "/api/client-errors",
                    headers={"Content-Type": "application/json"},
                    body="{",
                )
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)["error"], "invalid json")

                status, _, body = self.request_test_server(
                    db_path,
                    "POST",
                    "/api/client-errors",
                    headers={"Content-Type": "application/json"},
                    body="x" * (server.MAX_CLIENT_ERROR_BYTES + 1),
                )
                self.assertEqual(status, 413)
                self.assertEqual(json.loads(body)["error"], "payload too large")

    def test_request_performance_log_records_server_timing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            log_dir = f"{tmpdir}/logs"
            shutil.copy(server.DEFAULT_DB, db_path)

            with patch.dict(os.environ, {"ANIME_LOG_DIR": log_dir}):
                status, _, body = self.request_test_server(db_path, "GET", "/api/health")

            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"ok": True})
            entries = self.json_log_entries(log_dir, "performance.log")
            request_event = next(entry for entry in entries if entry["event"] == "server_request")
            self.assertEqual(request_event["method"], "GET")
            self.assertEqual(request_event["path"], "/api/health")
            self.assertEqual(request_event["status"], 200)
            self.assertGreaterEqual(request_event["duration_ms"], 0)
            self.assertGreater(request_event["response_bytes"], 0)

    def test_performance_endpoint_requires_auth_and_redacts_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            log_dir = f"{tmpdir}/logs"
            shutil.copy(server.DEFAULT_DB, db_path)
            user_id = self.create_google_user(db_path, "google-user-1", "one@example.com")
            token = self.create_session(db_path, user_id)
            payload = {
                "event": "home_boot",
                "path": "/",
                "duration_ms": 1234.5,
                "resources": [
                    {
                        "path": "https://kodikplayer.com/embed/private?token=secret",
                        "duration_ms": 999,
                    }
                ],
                "api_requests": [{"path": "/api/anime", "duration_ms": 712}],
                "context": {"token": "secret", "note": "ok"},
            }

            with patch.dict(os.environ, {"ANIME_LOG_DIR": log_dir}):
                status, _, body = self.request_test_server(
                    db_path,
                    "POST",
                    "/api/performance",
                    headers={"Content-Type": "application/json"},
                    body=json.dumps(payload),
                )
                self.assertEqual(status, 401)
                self.assertEqual(json.loads(body)["error"], "authentication required")

                status, _, body = self.request_test_server(
                    db_path,
                    "POST",
                    "/api/performance",
                    headers={
                        "Content-Type": "application/json",
                        "Cookie": f"{server.SESSION_COOKIE_NAME}={token}",
                    },
                    body=json.dumps(payload),
                )

            self.assertEqual(status, 202)
            self.assertEqual(json.loads(body), {"ok": True})
            entries = self.json_log_entries(log_dir, "performance.log")
            event = next(entry for entry in entries if entry["event"] == "home_boot")
            self.assertTrue(event["authenticated"])
            self.assertEqual(event["user_id"], user_id)
            self.assertEqual(event["duration_ms"], 1234.5)
            self.assertEqual(event["resources"][0]["path"], "<redacted-url>")
            self.assertEqual(event["context"]["token"], "<redacted>")
            self.assertEqual(event["api_requests"][0]["path"], "/api/anime")

    def test_unexpected_backend_exception_returns_500_and_logs_traceback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            log_dir = f"{tmpdir}/logs"
            shutil.copy(server.DEFAULT_DB, db_path)
            user_id = self.create_google_user(db_path, "google-user-1", "one@example.com")
            token = self.create_session(db_path, user_id)

            with patch.dict(os.environ, {"ANIME_LOG_DIR": log_dir}):
                with patch.object(server, "get_anime_list", side_effect=RuntimeError("boom")):
                    status, _, body = self.request_test_server(
                        db_path,
                        "GET",
                        "/api/anime",
                        headers={"Cookie": f"{server.SESSION_COOKIE_NAME}={token}"},
                    )

            self.assertEqual(status, 500)
            self.assertEqual(json.loads(body)["error"], "internal server error")
            server_log = (Path(log_dir) / "server.log").read_text(encoding="utf-8")
            self.assertIn("unhandled request error", server_log)
            self.assertIn("RuntimeError: boom", server_log)

    def test_google_redirect_post_sets_session_cookie_on_completion_page(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)
            body = urlencode(
                {
                    "credential": "fake-token",
                    "state": server.sign_google_auth_state("/some-title"),
                }
            )
            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
            }
            user_id = self.create_google_user(db_path, "handoff-redirect", "one@example.com")
            provisional_token = self.create_session(db_path, user_id)
            auth = {
                "user": {"id": user_id, "email": "one@example.com", "name": "One", "picture_url": None},
                "token": provisional_token,
                "expires_at": "2030-01-01T00:00:00+00:00",
            }

            with patch.object(server, "authenticate_google_credential", return_value=auth) as authenticate:
                status, headers, _ = self.request_test_server(
                    db_path,
                    "POST",
                    "/api/auth/google",
                    headers=headers,
                    body=body,
                )

            self.assertEqual(status, 302)
            complete_location = headers["Location"]
            self.assertEqual(urlparse(complete_location).path, "/api/auth/complete")
            self.assertNotIn("Set-Cookie", headers)
            authenticate.assert_called_once_with("fake-token", db_path)

            status, headers, response_body = self.request_test_server(
                db_path,
                "GET",
                complete_location,
            )

            self.assertEqual(status, 200)
            cookie_token = headers["Set-Cookie"].split("=", 1)[1].split(";", 1)[0]
            self.assertNotEqual(cookie_token, provisional_token)
            self.assertEqual(server.get_session_user(cookie_token, db_path)["id"], user_id)
            self.assertIsNone(server.get_session_user(provisional_token, db_path))
            csp = headers["Content-Security-Policy"]
            script_directive = next(
                directive for directive in csp.split("; ") if directive.startswith("script-src ")
            )
            self.assertNotIn("'unsafe-inline'", script_directive)
            nonce = script_directive.split("'nonce-", 1)[1].split("'", 1)[0]
            self.assertIn(f'nonce="{nonce}"'.encode(), response_body)
            self.assertIn(b'src="/static/client_errors.js"', response_body)
            self.assertIn(b"waitForSession", response_body)
            self.assertIn(b"const sessionDeadline = Date.now() + 12_000", response_body)
            self.assertIn(b"attempt < 120 && Date.now() < sessionDeadline", response_body)
            self.assertIn(b'fetch("/api/me"', response_body)
            self.assertIn(b'credentials: "same-origin"', response_body)
            self.assertIn(b"login.session_completion_timeout", response_body)
            self.assertIn(b"window.reportClientError", response_body)
            self.assertIn(b"Promise.race([report, delay(500)])", response_body)
            self.assertIn(b"window.location.replace", response_body)
            self.assertIn(b"/some-title", response_body)
            self.assertIn(b"auth_complete", response_body)

            status, headers, _ = self.request_test_server(
                db_path,
                "GET",
                complete_location,
            )

            self.assertEqual(status, 302)
            self.assertEqual(urlparse(headers["Location"]).path, "/login")

    def test_google_json_post_returns_completion_url_without_fetch_cookie(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)
            body = json.dumps({"credential": "fake-token", "next": "/wanted"})
            headers = {
                "Content-Type": "application/json",
            }
            user_id = self.create_google_user(db_path, "handoff-json", "one@example.com")
            provisional_token = self.create_session(db_path, user_id)
            auth = {
                "user": {"id": user_id, "email": "one@example.com", "name": "One", "picture_url": None},
                "token": provisional_token,
                "expires_at": "2030-01-01T00:00:00+00:00",
            }

            with patch.object(server, "authenticate_google_credential", return_value=auth) as authenticate:
                status, headers, response_body = self.request_test_server(
                    db_path,
                    "POST",
                    "/api/auth/google",
                    headers=headers,
                    body=body,
                )

            self.assertEqual(status, 200)
            self.assertNotIn("Set-Cookie", headers)
            payload = json.loads(response_body)
            self.assertEqual(payload["user"]["email"], "one@example.com")
            self.assertEqual(urlparse(payload["complete_url"]).path, "/api/auth/complete")
            authenticate.assert_called_once_with("fake-token", db_path)

            status, headers, response_body = self.request_test_server(
                db_path,
                "GET",
                payload["complete_url"],
            )

            self.assertEqual(status, 200)
            cookie_token = headers["Set-Cookie"].split("=", 1)[1].split(";", 1)[0]
            self.assertNotEqual(cookie_token, provisional_token)
            self.assertEqual(server.get_session_user(cookie_token, db_path)["id"], user_id)
            self.assertIsNone(server.get_session_user(provisional_token, db_path))
            self.assertIn(b"waitForSession", response_body)
            self.assertIn(b"/wanted", response_body)
            self.assertIn(b"auth_complete", response_body)

    def test_auth_config_returns_signed_google_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)

            with patch.object(server, "google_client_id", return_value="client.apps.googleusercontent.com"):
                status, _, body = self.request_test_server(
                    db_path,
                    "GET",
                    "/api/auth/config?next=%2Fwanted%3Ftab%3Dfavorites",
                )

            self.assertEqual(status, 200)
            payload = json.loads(body)
            self.assertEqual(payload["client_id"], "client.apps.googleusercontent.com")
            self.assertTrue(payload["state"])
            self.assertEqual(server.verify_google_auth_state(payload["state"]), "/wanted?tab=favorites")

    def test_google_auth_state_secret_is_stable_across_process_fallbacks(self):
        configured_secret = "shared-replica-secret-material-1234567890"
        with patch.dict(
            os.environ,
            {server.GOOGLE_AUTH_STATE_SECRET_ENV: configured_secret},
        ):
            state = server.sign_google_auth_state("/wanted")
            with patch.object(server, "GOOGLE_AUTH_STATE_FALLBACK_SECRET", b"other-process" * 3):
                self.assertEqual(server.verify_google_auth_state(state), "/wanted")

    def test_google_auth_state_rejects_weak_configured_secret(self):
        with patch.dict(
            os.environ,
            {server.GOOGLE_AUTH_STATE_SECRET_ENV: "too-short"},
        ):
            with self.assertRaisesRegex(server.AuthConfigError, "at least 32 bytes"):
                server.sign_google_auth_state("/wanted")

    def test_auth_config_reports_weak_state_secret_as_deployment_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)
            with (
                patch.dict(
                    os.environ,
                    {server.GOOGLE_AUTH_STATE_SECRET_ENV: "too-short"},
                ),
                patch.object(
                    server,
                    "google_client_id",
                    return_value="client.apps.googleusercontent.com",
                ),
            ):
                status, _, body = self.request_test_server(
                    db_path,
                    "GET",
                    "/api/auth/config",
                )

        self.assertEqual(status, 503)
        payload = json.loads(body)
        self.assertFalse(payload["configured"])
        self.assertEqual(payload["state"], "")
        self.assertIn(server.GOOGLE_AUTH_STATE_SECRET_ENV, payload["error"])

    def test_safe_next_path_rejects_external_and_relative_targets(self):
        self.assertEqual(server.safe_next_path("/wanted?tab=favorites#details"), "/wanted?tab=favorites#details")
        self.assertEqual(server.safe_next_path("relative/path"), "/")
        self.assertEqual(server.safe_next_path("//evil.example/path"), "/")
        self.assertEqual(server.safe_next_path("https://evil.example/path"), "/")
        self.assertEqual(server.safe_next_path("/%5cevil.example/path"), "/")
        self.assertNotIn("</script>", server.inline_script_json("/</script><script>boom</script>"))

    def test_google_auth_complete_rejects_invalid_handoff_code(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)

            status, headers, _ = self.request_test_server(
                db_path,
                "GET",
                "/api/auth/complete?code=missing",
            )

            self.assertEqual(status, 302)
            self.assertEqual(urlparse(headers["Location"]).path, "/login")
            self.assertIn("auth_error", parse_qs(urlparse(headers["Location"]).query))

    def test_login_handoff_stores_no_recoverable_session_token(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            user_id = self.create_google_user(db_path, "secure-handoff", "secure@example.com")
            provisional_token = self.create_session(db_path, user_id)

            code = server.create_login_handoff(provisional_token, "/wanted", db_path)

            raw = sqlite3.connect(db_path)
            try:
                columns = {row[1] for row in raw.execute("pragma table_info(login_handoffs)")}
                handoff = raw.execute("select * from login_handoffs").fetchone()
                provisional_count = raw.execute(
                    "select count(*) from sessions where token_hash = ?",
                    (server.session_token_hash(provisional_token),),
                ).fetchone()[0]
            finally:
                raw.close()
            self.assertNotIn("session_token", columns)
            self.assertIsNotNone(handoff)
            self.assertEqual(provisional_count, 0)
            for candidate in (Path(db_path), Path(f"{db_path}-wal")):
                if candidate.exists():
                    self.assertNotIn(provisional_token.encode(), candidate.read_bytes())

            token, next_path = server.consume_login_handoff(code, db_path)
            self.assertNotEqual(token, provisional_token)
            self.assertEqual(next_path, "/wanted")
            self.assertEqual(server.get_session_user(token, db_path)["id"], user_id)

    def test_invalid_login_handoff_does_not_wait_for_sqlite_writer_lock(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            server.prepare_database(db_path)
            blocker = sqlite3.connect(db_path)
            try:
                blocker.execute("begin immediate")
                started = time.perf_counter()
                with self.assertRaises(server.AuthError):
                    server.consume_login_handoff("definitely-missing", db_path)
                elapsed = time.perf_counter() - started
            finally:
                blocker.rollback()
                blocker.close()
            self.assertLess(elapsed, 0.5)

    def test_google_redirect_post_redirects_invalid_state_to_login(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)
            body = urlencode({"credential": "fake-token", "state": "invalid-state"})
            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
            }

            status, headers, response_body = self.request_test_server(
                db_path,
                "POST",
                "/api/auth/google",
                headers=headers,
                body=body,
            )

            self.assertEqual(status, 302)
            location = headers["Location"]
            self.assertEqual(urlparse(location).path, "/login")
            self.assertEqual(
                parse_qs(urlparse(location).query)["auth_error"],
                [server.GOOGLE_AUTH_STATE_ERROR],
            )
            self.assertEqual(response_body, b"")

    def test_google_redirect_post_redirects_config_error_to_login(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)
            body = urlencode(
                {
                    "credential": "fake-token",
                    "state": server.sign_google_auth_state("/wanted"),
                }
            )
            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
            }

            with patch.object(
                server,
                "authenticate_google_credential",
                side_effect=server.AuthConfigError("google-auth dependencies are not installed"),
            ):
                status, headers, _ = self.request_test_server(
                    db_path,
                    "POST",
                    "/api/auth/google",
                    headers=headers,
                    body=body,
                )

            self.assertEqual(status, 302)
            location = headers["Location"]
            self.assertEqual(urlparse(location).path, "/login")
            params = parse_qs(urlparse(location).query)
            self.assertEqual(params["next"], ["/wanted"])
            self.assertEqual(
                params["auth_error"],
                ["Ошибка конфигурации деплоймента: google-auth dependencies are not installed"],
            )

    def test_new_google_user_starts_empty_and_existing_state_persists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)

            con = server.connect(db_path)
            try:
                con.execute("delete from sessions")
                con.execute("delete from user_title_state")
                con.execute("delete from users")
                anime_id = con.execute("select id from anime order by id limit 1").fetchone()["id"]
                con.commit()
            finally:
                con.close()

            profile = {
                "sub": "brand-new-google-user",
                "email": "new@example.com",
                "email_verified": True,
                "name": "New User",
                "picture": None,
            }
            with patch.object(server, "verify_google_credential", return_value=profile):
                auth = server.authenticate_google_credential("fake-token", db_path)

            new_user_id = auth["user"]["id"]
            con = server.connect(db_path)
            try:
                new_count = con.execute(
                    "select count(*) from user_title_state where user_id = ?",
                    (new_user_id,),
                ).fetchone()[0]
            finally:
                con.close()

            self.assertEqual(new_count, 0)

            new_detail = server.get_anime_detail(anime_id, db_path, new_user_id)
            self.assertFalse(new_detail["is_favorite"])
            self.assertIsNone(new_detail["progress_episode_number"])

            server.update_user_state(
                anime_id,
                {"is_favorite": True, "progress_episode_number": 2},
                db_path,
                new_user_id,
            )
            with patch.object(server, "verify_google_credential", return_value=profile):
                server.authenticate_google_credential("fake-token", db_path)

            new_detail = server.get_anime_detail(anime_id, db_path, new_user_id)
            self.assertTrue(new_detail["is_favorite"])
            self.assertEqual(new_detail["progress_episode_number"], 2)

    def test_old_user_state_schema_is_dropped_without_local_profile_import(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)

            con = sqlite3.connect(db_path)
            try:
                anime_id = con.execute("select id from anime order by id limit 1").fetchone()[0]
                con.execute("drop table user_title_state")
                con.execute(
                    """
                    create table user_title_state (
                        anime_id integer primary key,
                        is_favorite integer not null default 0,
                        progress_episode_number integer,
                        watched integer not null default 0,
                        updated_at text not null
                    )
                    """
                )
                con.execute(
                    "insert into user_title_state values (?, 1, 5, 0, ?)",
                    (anime_id, "2026-07-06T10:00:00+00:00"),
                )
                con.commit()
            finally:
                con.close()

            con = server.connect(db_path)
            try:
                columns = [row[1] for row in con.execute("pragma table_info(user_title_state)")]
                self.assertIn("user_id", columns)
                self.assertEqual(con.execute("select count(*) from user_title_state").fetchone()[0], 0)
            finally:
                con.close()

    def test_user_state_is_scoped_per_google_user(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)

            user_one = self.create_google_user(db_path, "google-user-1", "one@example.com")
            user_two = self.create_google_user(db_path, "google-user-2", "two@example.com")
            anime_id = server.get_anime_list(db_path, user_id=user_one)[0]["id"]

            server.update_user_state(
                anime_id,
                {"is_favorite": True, "progress_episode_number": 3},
                db_path,
                user_one,
            )
            server.update_user_state(anime_id, {"watched": True}, db_path, user_two)

            detail_one = server.get_anime_detail(anime_id, db_path, user_one)
            detail_two = server.get_anime_detail(anime_id, db_path, user_two)
            self.assertTrue(detail_one["is_favorite"])
            self.assertEqual(detail_one["progress_episode_number"], 3)
            self.assertFalse(detail_one["watched"])
            self.assertFalse(detail_two["is_favorite"])
            self.assertIsNone(detail_two["progress_episode_number"])
            self.assertTrue(detail_two["watched"])

            rec_one = server.get_recommendations(db_path, user_id=user_one)
            rec_two = server.get_recommendations(db_path, user_id=user_two)
            self.assertEqual(rec_one["profile"]["mode"], "personalized")
            self.assertEqual(rec_two["profile"]["mode"], "personalized")
            self.assertEqual(rec_one["profile"]["favorite_count"], 1)
            self.assertEqual(rec_two["profile"]["favorite_count"], 0)

    def test_recommendations_are_ranked_and_exclude_known_titles(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)
            user_id = self.create_google_user(db_path, "google-user-1", "one@example.com")

            items = server.get_anime_list(db_path, user_id=user_id)
            favorite = next(item for item in items if item.get("genres") and item.get("source_count") > 0)
            server.update_user_state(favorite["id"], {"is_favorite": True}, db_path, user_id)

            payload = server.get_recommendations(db_path, limit=20, user_id=user_id)
            recommendations = payload["items"]
            self.assertGreater(len(recommendations), 0)
            self.assertLessEqual(len(recommendations), 20)
            self.assertTrue(all(item["source_count"] > 0 for item in recommendations))
            self.assertNotIn(favorite["id"], {item["id"] for item in recommendations})
            self.assertEqual(
                [item["recommendation_score"] for item in recommendations],
                sorted((item["recommendation_score"] for item in recommendations), reverse=True),
            )
            self.assertTrue(all(item["recommendation_reasons"] for item in recommendations))
            self.assertEqual(payload["profile"]["mode"], "personalized")

    def test_recommendations_prioritize_watchable_candidates(self):
        payload = server.get_recommendations(limit=server.MAX_RECOMMENDATION_LIMIT)
        recommendations = payload["items"]
        watchable_count = payload["profile"]["watchable_candidate_count"]
        if watchable_count:
            priority_count = min(watchable_count, len(recommendations))
            self.assertTrue(all(item["source_count"] > 0 for item in recommendations[:priority_count]))
            self.assertTrue(all(
                item["recommendation_components"]["watchable"] == 1.0
                for item in recommendations[:priority_count]
            ))
        self.assertEqual(
            [item["recommendation_score"] for item in recommendations],
            sorted((item["recommendation_score"] for item in recommendations), reverse=True),
        )

    def test_recommendations_have_popular_fallback_without_profile(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)
            con = server.connect(db_path)
            con.execute("delete from user_title_state")
            con.commit()
            con.close()

            payload = server.get_recommendations(db_path, limit=999)
            recommendations = payload["items"]
            self.assertEqual(payload["limit"], server.MAX_RECOMMENDATION_LIMIT)
            self.assertEqual(payload["profile"]["mode"], "cold_start")
            self.assertGreater(len(recommendations), 0)
            self.assertLessEqual(len(recommendations), server.MAX_RECOMMENDATION_LIMIT)
            self.assertTrue(all(item["source_count"] > 0 for item in recommendations))
            self.assertTrue(all("recommendation_components" in item for item in recommendations))

            fallback = server.get_recommendations(db_path, limit="not-a-number")
            self.assertEqual(fallback["limit"], server.DEFAULT_RECOMMENDATION_LIMIT)

    def test_player_markup_allows_fullscreen_and_picture_in_picture(self):
        html = Path(server.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="player"', html)
        self.assertIn('id="content-source"', html)
        self.assertIn("allowfullscreen", html)
        self.assertIn("fullscreen", html)
        self.assertIn("picture-in-picture", html)
        self.assertIn("web-share", html)
        self.assertIn("screen-wake-lock", html)
        self.assertIn('id="fullscreen-toggle"', html)
        self.assertIn('id="pip-toggle"', html)
        self.assertIn('id="recommendation-meta"', html)
        self.assertIn('href="/static/favicon.svg"', html)
        self.assertIn('href="/favicon.ico"', html)
        self.assertIn('src="/static/client_errors.js"', html)
        self.assertNotIn('href="/admin"', html)
        self.assertNotIn('id="admin-link"', html)

    def test_login_uses_google_one_tap_and_button(self):
        html = Path(server.STATIC_DIR / "login.html").read_text(encoding="utf-8")
        js = Path(server.STATIC_DIR / "login.js").read_text(encoding="utf-8")
        reporter_js = Path(server.STATIC_DIR / "client_errors.js").read_text(encoding="utf-8")
        self.assertIn("https://accounts.google.com/gsi/client?hl=ru", html)
        self.assertIn('src="/static/client_errors.js"', html)
        self.assertLess(
            html.index('src="/static/client_errors.js"'),
            html.index("https://accounts.google.com/gsi/client?hl=ru"),
        )
        self.assertIn('id="one-tap-anchor"', html)
        self.assertIn('id="google-button"', html)
        self.assertIn("google.accounts.id.prompt", js)
        self.assertIn("function authError()", js)
        self.assertIn('get("auth_error")', js)
        self.assertIn("api/auth/config?next=", js)
        self.assertIn("const recoveringSession = !redirectError && recoverExistingSession()", js)
        self.assertIn("auto_select: !recoveringSession", js)
        self.assertIn("itp_support: true", js)
        self.assertIn("callback: handleCredential", js)
        self.assertIn('fetch("/api/auth/google"', js)
        self.assertIn('credentials: "same-origin"', js)
        self.assertIn("complete_url", js)
        self.assertIn("authComplete()", js)
        self.assertIn("recoverExistingSession", js)
        self.assertIn("startSessionWatcher", js)
        self.assertIn("checkExistingSession", js)
        self.assertIn("LOGIN_SESSION_POLL_INTERVAL_MS", js)
        self.assertIn('window.addEventListener("focus"', js)
        self.assertIn('window.addEventListener("pageshow"', js)
        self.assertIn('document.addEventListener("visibilitychange"', js)
        self.assertIn("sessionStorage.setItem(LOGIN_RECOVERY_STARTED_KEY", js)
        self.assertIn("credentialSubmitInFlight", js)
        self.assertNotIn("window.location.reload()", js)
        self.assertNotIn('ux_mode: "redirect"', js)
        self.assertNotIn("login_uri:", js)
        self.assertNotIn("use_fedcm_for_button", js)
        self.assertNotIn("button_auto_select", js)
        self.assertIn("google.accounts.id.renderButton", js)
        self.assertIn('const GOOGLE_LOCALE = "ru"', js)
        self.assertIn("locale: GOOGLE_LOCALE", js)
        self.assertIn("click_listener: handleGoogleButtonClick", js)
        self.assertIn("state: config.state", js)
        self.assertIn("if (!recoveringSession) maybeShowOneTap(google, Boolean(redirectError))", js)
        self.assertIn("renderUnavailableGoogleButton", js)
        self.assertIn("google-fallback-button", js)
        self.assertIn("Ошибка конфигурации деплоймента", js)
        self.assertIn("login.session_recovery_timeout", js)
        self.assertIn("lastSessionCheckStatus = response.status", js)
        self.assertIn("/api/client-errors", reporter_js)
        self.assertIn("window.addEventListener(\"error\"", reporter_js)
        self.assertIn("window.addEventListener(\"unhandledrejection\"", reporter_js)
        self.assertIn('document.addEventListener("securitypolicyviolation"', reporter_js)
        self.assertIn("Content Security Policy blocked", reporter_js)

    def test_app_boot_parallelizes_initial_api_and_defers_recommendations(self):
        js = Path(server.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        boot = js[js.index("async function boot()"):js.index("boot().catch")]
        self.assertIn("Promise.all", boot)
        self.assertIn('api("/api/me")', boot)
        self.assertIn('api("/api/app-config")', boot)
        self.assertIn('api("/api/anime")', boot)
        self.assertIn('markPerformanceCheckpoint("recommendations_deferred")', boot)
        self.assertNotIn("await loadRecommendations", boot)
        self.assertNotIn("/api/recommendations", boot)

    def test_login_page_allows_google_popup_opener(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)

            status, headers, body = self.request_test_server(db_path, "GET", "/login")

            self.assertEqual(status, 200)
            self.assertIn(b"accounts.google.com/gsi/client", body)
            self.assertEqual(headers["Cross-Origin-Opener-Policy"], "same-origin-allow-popups")
            self.assertEqual(headers["Referrer-Policy"], "strict-origin-when-cross-origin")

    def test_security_headers_allow_exact_player_hosts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            server.prepare_database(db_path)
            status, headers, _ = self.request_test_server(db_path, "GET", "/api/health")

        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        csp = headers["Content-Security-Policy"]
        script_directive = next(
            directive for directive in csp.split("; ") if directive.startswith("script-src ")
        )
        self.assertNotIn("'unsafe-inline'", script_directive)
        for host in server.PLAYER_HOSTS:
            self.assertIn(f"https://{host}", csp)
            self.assertIn(f"https://*.{host}", csp)

    def test_player_host_allowlist_matches_frontend_and_catalog(self):
        app_js = Path(server.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn('api("/api/app-config")', app_js)
        self.assertIn("appConfig.player_hosts", app_js)
        self.assertNotIn("const PLAYER_HOSTS", app_js)

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)
            user_id = self.create_google_user(db_path, "app-config", "config@example.com")
            token = self.create_session(db_path, user_id)
            status, _, body = self.request_test_server(
                db_path,
                "GET",
                "/api/app-config",
                headers={"Cookie": f"{server.SESSION_COOKIE_NAME}={token}"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["player_hosts"], list(server.PLAYER_HOSTS))

        con = server.connect()
        try:
            catalog_hosts = {
                str(row[0]).strip().lower().split(":", 1)[0]
                for row in con.execute(
                    "select distinct embed_host from video_sources where embed_host is not null"
                )
                if str(row[0]).strip()
            }
        finally:
            con.close()
        for host in catalog_hosts:
            self.assertTrue(
                any(host == allowed or host.endswith(f".{allowed}") for allowed in server.PLAYER_HOSTS),
                host,
            )

    def test_static_path_cannot_escape_static_directory_by_prefix_collision(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "static"
            root.mkdir()
            (Path(tmpdir) / "static-secret.txt").write_text("secret", encoding="utf-8")
            with patch.object(server, "STATIC_DIR", root):
                status, _, body = self.request_test_server(
                    str(Path(tmpdir) / "unused.sqlite"),
                    "GET",
                    "/static/../static-secret.txt",
                )
        self.assertEqual(status, 404)
        self.assertNotIn(b"secret", body)

    def test_broken_pipe_is_not_reported_as_unhandled_500(self):
        handler = object.__new__(server.AnimeHandler)
        handler.client_address = ("127.0.0.1", 12345)
        handler.command = "GET"
        handler.path = "/api/anime"

        def disconnect():
            raise BrokenPipeError("client disconnected")

        with (
            patch.object(handler, "send_unexpected_error") as unexpected,
            patch.object(handler, "log_request_performance") as performance,
        ):
            handler.handle_request(disconnect)
        unexpected.assert_not_called()
        self.assertIsInstance(performance.call_args.args[1], BrokenPipeError)

    def test_view_mode_tabs_use_compact_accessible_labels(self):
        html = Path(server.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn('aria-label="Режим каталога"', html)
        self.assertIn('class="view-tab-icon"', html)
        self.assertIn('class="view-tab-label">Избр.</span>', html)
        self.assertIn('aria-label="Избранное"', html)
        self.assertIn('class="view-tab-label">Смотрю</span>', html)
        self.assertNotIn('class="view-tab-label">Буду</span>', html)
        self.assertNotIn('class="view-tab-label">Готово</span>', html)
        self.assertNotIn('class="view-tab-label">Брошено</span>', html)
        self.assertIn('aria-pressed="true"', html)

    def test_mobile_watch_actions_are_compact_and_consistent(self):
        html = Path(server.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        css = Path(server.STATIC_DIR / "app.css").read_text(encoding="utf-8")
        js = Path(server.STATIC_DIR / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="favorite-toggle" class="favorite-toggle"', html)
        self.assertIn('<fieldset id="watch-status-control" class="watch-status-control">', html)
        self.assertIn('<legend class="visually-hidden">Статус просмотра</legend>', html)
        self.assertEqual(html.count('type="radio" name="watch-status"'), 3)
        self.assertIn('name="watch-status" value="none" checked', html)
        self.assertIn('name="watch-status" value="watching"', html)
        self.assertIn('name="watch-status" value="completed"', html)
        self.assertIn('<span>○ Не смотрю</span>', html)
        self.assertIn('<span>▶ Смотрю</span>', html)
        self.assertIn('<span>✓ Просмотрено</span>', html)
        self.assertNotIn('id="not-watching-button"', html)
        self.assertNotIn('id="watched-toggle"', html)
        self.assertNotIn('id="not-interested-button"', html)
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr));", css)
        self.assertIn(".watch-status-option:has(input:checked)", css)
        self.assertIn('.watch-status-option[data-watch-status="watching"]:has(input:checked)', css)
        self.assertIn('.watch-status-option[data-watch-status="completed"]:has(input:checked)', css)
        self.assertIn(".watch-status-option:has(input:disabled)", css)
        self.assertIn(".library-controls {", css)
        self.assertIn(".watch-status-control {", css)
        self.assertNotIn(".watch-row:has(", css)
        self.assertIn('document.querySelectorAll(\'input[name="watch-status"]\')', js)
        self.assertIn("for (const input of el.watchStatusInputs)", js)
        self.assertIn("function canMarkTitleCompleted(item)", js)
        self.assertIn('input.value === "completed" && !canMarkTitleCompleted(detail)', js)
        self.assertIn("input.disabled = unavailable", js)
        self.assertIn("Недоступно, пока тайтл выходит", js)
        self.assertIn('watch_status: "none"', js)
        self.assertIn('detail.is_favorite ? "★ В избранном" : "☆ В избранное"', js)
        self.assertNotIn("notInterestedButton", js)
        self.assertNotIn('classList.add("not-interested")', js)
        self.assertNotIn('${item.not_interested ? "⊘ " : ""}', js)

        favorite_start = js.index('el.favoriteToggle.addEventListener("click"')
        favorite_end = js.index("for (const input of el.watchStatusInputs)", favorite_start)
        favorite_handler = js[favorite_start:favorite_end]
        self.assertIn("is_favorite: !state.detail.is_favorite", favorite_handler)
        self.assertNotIn("watch_status", favorite_handler)

    def test_admin_page_has_dashboard_assets(self):
        html = Path(server.STATIC_DIR / "admin.html").read_text(encoding="utf-8")
        js = Path(server.STATIC_DIR / "admin.js").read_text(encoding="utf-8")
        self.assertIn('body class="admin-page"', html)
        self.assertIn('id="admin-summary"', html)
        self.assertIn('id="admin-users"', html)
        self.assertIn('id="admin-top-titles"', html)
        self.assertIn('id="admin-recent-activity"', html)
        self.assertIn("Серии", html)
        self.assertIn("по последней активности", js)
        self.assertIn("Серий начато", js)
        self.assertIn("Вероятно досмотрено", js)
        self.assertIn('id="admin-telemetry-scope"', html)
        self.assertIn('scope="col"', html)
        self.assertNotIn("С активной сессией", js)
        self.assertIn('src="/static/admin.js"', html)
        self.assertIn("/api/admin/users", js)
        self.assertIn("is_admin", js)

    def test_right_pane_deep_links_are_supported(self):
        js = Path(server.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn("readLinkState", js)
        self.assertIn("syncUrlFromDetail", js)
        self.assertIn("window.history", js)

    def test_frontend_tracks_watch_events_from_iframe_boundary(self):
        js = Path(server.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn('const WATCH_ENDPOINT = "/api/watch-events"', js)
        self.assertIn('api("/api/continue-watching")', js)
        self.assertIn('el.player.addEventListener("focus", handlePlayerEngaged)', js)
        self.assertIn('window.addEventListener("message", handlePlayerMessage)', js)
        self.assertIn("event.source !== el.player.contentWindow", js)
        self.assertIn("event.origin === context.playerOrigin", js)
        self.assertIn('message?.provider !== "kodik"', js)
        self.assertIn("frontendRuntime.playerMessageProvider(source)", js)
        self.assertIn("providerPlaybackEngagedSeconds", js)
        self.assertIn("WATCH_FALLBACK_ENGAGEMENT_DELAY_MS", js)
        self.assertIn('markWatchEngaged("pip_open")', js)
        self.assertIn("playbackWasActive", js)
        self.assertIn("preservePlayer: true", js)
        self.assertIn("const contextMatches = Boolean(", js)
        self.assertIn("if (contextMatches) {", js)
        self.assertIn("const reportedBy = `${provider}_player_api`", js)
        self.assertIn("fallbackFocused = !state.playerContext?.messageProvider", js)
        self.assertIn('sendWatchEvent("heartbeat"', js)
        self.assertNotIn("manuallyCorrected", js)

    def test_content_update_navigation_does_not_start_watching(self):
        js = Path(server.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        select_start = js.index("async function selectEpisode")
        select_end = js.index("function userStateTargets", select_start)
        select_episode = js[select_start:select_end]
        update_start = js.index("async function openContentUpdateEvent")
        update_end = js.index('el.search.addEventListener("input"', update_start)
        open_update = js[update_start:update_end]

        self.assertIn("remember = true", select_episode)
        self.assertIn("if (remember) await saveTitleNavigation(id)", select_episode)
        self.assertNotIn("saveUserState", select_episode)
        self.assertNotIn("progress_episode_number", select_episode)
        self.assertNotIn("persist: false", open_update)

    def test_content_source_switch_moves_to_nearest_available_episode(self):
        js = Path(server.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        handler_start = js.index('el.contentSource.addEventListener("change"')
        handler_end = js.index('el.translation.addEventListener("change"', handler_start)
        handler = js[handler_start:handler_end]

        self.assertIn("nearestEpisodeIdForContentSource(selectedContentSource)", handler)
        self.assertIn("state.selectedEpisodeId = selectedEpisodeId", handler)
        self.assertIn("renderEpisodes(state.detail)", handler)
        self.assertIn("saveTitleNavigation()", handler)
        self.assertNotIn("persistCurrentEpisodeSelection()", handler)

    def test_frontend_content_update_filters_refresh_report_in_priority_time_order(self):
        js = Path(server.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        start = js.index("function applyFilter")
        end = js.index("async function loadSearchFields", start)
        apply_filter = js[start:end]

        self.assertIn("if (isUpdatesView()) {", apply_filter)
        self.assertIn("compareContentUpdates(left.item, right.item)", apply_filter)
        self.assertLess(
            apply_filter.index("compareContentUpdates(left.item, right.item)"),
            apply_filter.index("left.searchScore !== right.searchScore"),
        )
        self.assertIn("if (isUpdatesView()) renderContentUpdatesView();", apply_filter)
        self.assertIn("const orderedItems = [", js)
        self.assertIn("...items.filter(item => item.is_priority)", js)
        self.assertIn("function contentUpdateHasUnseenEpisode(item)", js)
        self.assertIn("function contentUpdateItemIsPriority(item)", js)
        self.assertIn('effectiveWatchStatus(item) === "completed"', js)
        self.assertIn("number > progress", js)
        self.assertIn('heading.textContent = "Новое для меня · избранное и смотрю"', js)
        self.assertIn('const CONTENT_UPDATE_DEFAULT_DAYS = "7"', js)
        self.assertIn('russianPlural(days, "день", "дня", "дней")', js)

        compare_start = js.index("function compareContentUpdates")
        compare_end = js.index("function filterOptionLabel", compare_start)
        compare_updates = js[compare_start:compare_end]
        self.assertIn("contentUpdateItemIsPriority(left)", compare_updates)
        self.assertIn("contentUpdateItemIsPriority(right)", compare_updates)
        self.assertNotIn("Boolean(left.is_favorite)", compare_updates)

    def test_frontend_opens_last_selected_episode_without_changing_progress(self):
        js = Path(server.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        html = Path(server.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn("function episodeIdForProgress(progressEpisodeNumber)", js)
        self.assertIn("function episodeIdForLastWatch(lastWatch)", js)
        self.assertIn("function episodeIdForLastOpened(lastOpened)", js)
        self.assertIn("last_opened_episode", js)
        self.assertIn("/navigation", js)
        self.assertIn("function effectiveProgressEpisodeNumber(detail)", js)
        self.assertIn("numberFrom(episode.number) === progress", js)
        self.assertIn("effectiveProgressEpisodeNumber(detail)", js)
        self.assertNotIn("progress-episode", html)
        self.assertNotIn("progress-summary", html)
        self.assertNotIn('document.getElementById("progress-episode")', js)
        self.assertNotIn("el.progressEpisode", js)
        self.assertIn('id="watch-status-control"', html)
        self.assertIn('name="watch-status" value="none"', html)
        self.assertNotIn('id="not-watching-button"', html)
        self.assertNotIn('id="watched-toggle"', html)
        self.assertNotIn('id="not-interested-button"', html)
        self.assertIn('document.querySelectorAll(\'input[name="watch-status"]\')', js)
        self.assertIn("function discardWatchSession", js)
        self.assertIn("progress_episode_number: null", js)
        self.assertIn('status !== "watching"', js)
        self.assertIn("library_watch_status: effectiveWatchStatus(libraryState)", js)
        self.assertIn("library_watch_status_updated_at:", js)
        self.assertIn("effectiveProgressEpisodeNumber(state.detail) ?? numberFrom(activeEpisode()?.number)", js)

        start = js.index("function applyDetailLinkState")
        end = js.index("function numericValue", start)
        apply_detail = js[start:end]
        self.assertIn("episodeIdForLastWatch(lastWatch)", apply_detail)
        selected_start = apply_detail.index("state.selectedEpisodeId")
        selected_end = apply_detail.index("state.selectedContentSource", selected_start)
        selected_chain = apply_detail[selected_start:selected_end]
        explicit_index = selected_chain.index("explicitEpisodeId")
        last_opened_index = selected_chain.index("lastOpenedEpisodeId")
        last_watch_index = selected_chain.index("lastWatchEpisodeId")
        progress_index = selected_chain.index("episodeIdForProgress(state.detail.progress_episode_number)")
        first_available_index = selected_chain.index("firstAvailable || state.detail.episodes[0]")
        self.assertLess(explicit_index, last_opened_index)
        self.assertLess(last_opened_index, last_watch_index)
        self.assertLess(last_watch_index, progress_index)
        self.assertLess(progress_index, first_available_index)
        self.assertIn("lastWatch?.video_source_id", apply_detail)

        select_start = js.index("async function selectEpisode")
        select_end = js.index("function userStateTargets", select_start)
        select_episode = js[select_start:select_end]
        self.assertIn("saveTitleNavigation(id)", select_episode)
        self.assertNotIn("saveUserState", select_episode)
        self.assertNotIn("progress_episode_number", select_episode)

    def test_frontend_progress_view_excludes_watched_titles(self):
        js = Path(server.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        start = js.index("function itemMatchesView")
        end = js.index("function currentShelfTotal", start)
        view_match = js[start:end]

        self.assertIn('mode === "progress"', view_match)
        self.assertIn('status === "watching"', view_match)
        self.assertNotIn('status === "paused"', view_match)
        self.assertNotIn('mode === "planned"', view_match)
        self.assertNotIn('mode === "completed"', view_match)
        self.assertNotIn('mode === "dropped"', view_match)
        self.assertNotIn("item.watched || item.progress_episode_number != null", view_match)

    def test_frontend_favorites_sort_by_watch_status_then_recent_update(self):
        js = Path(server.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        compare_start = js.index("function favoriteWatchStatusRank")
        compare_end = js.index("function compareRecommendations", compare_start)
        favorite_compare = js[compare_start:compare_end]
        self.assertIn('status === "watching"', favorite_compare)
        self.assertIn('status === "completed"', favorite_compare)
        self.assertIn("favoriteWatchStatusRank(left) - favoriteWatchStatusRank(right)", favorite_compare)
        self.assertIn("Number(hasRecentUpdates(right)) - Number(hasRecentUpdates(left))", favorite_compare)

        filter_start = js.index("function applyFilter")
        filter_end = js.index("async function loadSearchFields", filter_start)
        apply_filter = js[filter_start:filter_end]
        priority_call = 'compareFavoritePriority(left.item, right.item)'
        self.assertIn('state.viewMode === "favorites"', apply_filter)
        self.assertIn(priority_call, apply_filter)
        self.assertLess(
            apply_filter.index(priority_call),
            apply_filter.index("left.searchScore !== right.searchScore"),
        )

    def test_frontend_hides_episode_count_meta_from_title_cards(self):
        js = Path(server.STATIC_DIR / "app.js").read_text(encoding="utf-8")

        start = js.index("function progressText")
        end = js.index("function effectiveProgressEpisodeNumber", start)
        progress_text = js[start:end]
        self.assertIn("effectiveWatchStatus(item)", progress_text)
        self.assertIn("watchStatusLabel(status)", progress_text)
        self.assertNotIn("effectiveProgressEpisodeNumber", progress_text)
        self.assertIn("серия", progress_text)

        start = js.index("function renderList")
        end = js.index("function renderRecommendationMeta", start)
        render_list = js[start:end]
        self.assertNotIn("item.episodes_text", render_list)

        start = js.index("function renderDetail")
        end = js.index("function createEpisodeNavButton", start)
        render_detail = js[start:end]
        self.assertNotIn("detail.episodes_text", render_detail)

    def test_frontend_recommendations_use_request_generation_for_race_safety(self):
        js = Path(server.STATIC_DIR / "app.js").read_text(encoding="utf-8")

        self.assertIn("recommendationsRequestId: 0", js)
        self.assertIn("function invalidateRecommendations()", js)
        self.assertIn("state.recommendationsRequestId += 1", js)

        start = js.index("async function loadRecommendations")
        end = js.index("function ensureRecommendations", start)
        load_recommendations = js[start:end]
        self.assertIn("requestId !== state.recommendationsRequestId", load_recommendations)
        self.assertIn("return state.recommendations", load_recommendations)

        start = js.index("function ensureRecommendations")
        end = js.index("function loadRecommendationsForView", start)
        ensure_recommendations = js[start:end]
        self.assertIn("if (!force && state.recommendationsLoading)", ensure_recommendations)
        self.assertNotIn("if (state.recommendationsLoading) return", ensure_recommendations)
        self.assertIn("const requestId = state.recommendationsRequestId + 1", ensure_recommendations)
        self.assertIn("state.recommendationsLoading === request", ensure_recommendations)

    def test_frontend_recommendations_reload_after_watch_state_changes_in_view(self):
        js = Path(server.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        start = js.index("function applyWatchState")
        end = js.index("function postWatchPayload", start)
        apply_watch_state = js[start:end]

        self.assertIn("invalidateRecommendations();", apply_watch_state)
        self.assertIn("if (isRecommendationView()) loadRecommendationsForView({ force: true, selectFirst: false });", apply_watch_state)

        start = js.index("function loadRecommendationsForView")
        end = js.index("el.search.addEventListener", start)
        load_for_view = js[start:end]
        self.assertIn("function loadRecommendationsForView({ force = false, selectFirst = true } = {})", load_for_view)
        self.assertIn("const request = ensureRecommendations({ force });", load_for_view)
        self.assertIn("const requestId = state.recommendationsRequestId;", load_for_view)
        self.assertIn("if (requestId !== state.recommendationsRequestId) return;", load_for_view)
        self.assertIn("applyFilter({ selectFirst });", load_for_view)
        self.assertNotIn("applyFilter({ selectFirst: true });", load_for_view)
        self.assertIn("reportActionError(\"load recommendations\")(error);", load_for_view)
        self.assertIn("if (isRecommendationView()) applyFilter();", load_for_view)

    def test_admin_link_is_created_only_after_admin_user_payload(self):
        js = Path(server.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn("function renderAdminLink", js)
        self.assertIn('link.href = "/admin"', js)
        self.assertIn("!state.user?.is_admin", js)

    def test_right_pane_state_save_does_not_autoselect_first_filtered_item(self):
        js = Path(server.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        start = js.index("async function saveUserState")
        end = js.index("function applyFilter", start)
        save_user_state = js[start:end]

        self.assertIn("applyFilter();", save_user_state)
        self.assertIn("loadRecommendationsForView({ force: true, selectFirst: false });", save_user_state)
        self.assertNotIn("applyFilter({ selectFirst", save_user_state)
        self.assertNotIn("selectFirst: true", save_user_state)

    def test_yummyanime_mushoku_titles_are_available(self):
        items = server.get_anime_list()
        yummy = [item for item in items if item.get("source") == "yummyanime"]
        self.assertGreaterEqual(len(yummy), 4)
        self.assertTrue(any(item["title"] == "Реинкарнация безработного 3 сезон" for item in yummy))
        self.assertTrue(any(item["source_count"] > 0 for item in yummy))

        detail = server.get_anime_detail(next(item["id"] for item in yummy if item["source_count"] > 0))
        labels = {field["label"] for field in detail["fields"]}
        self.assertIn("Источник", labels)
        self.assertEqual(detail["source"], "yummyanime")

        season3 = server.get_anime_detail(next(item["id"] for item in yummy if item["title"] == "Реинкарнация безработного 3 сезон"))
        source_rows = [
            source
            for sources in season3["sources_by_episode"].values()
            for source in sources
        ]
        self.assertTrue(source_rows)
        self.assertEqual(source_rows[0]["provider_title"], "Kodik")
        self.assertFalse(any(source["provider_title"] == "Alloha" for source in source_rows))

        episode_urls = {}
        for episode in season3["episodes"]:
            sources = season3["sources_by_episode"].get(episode["id"], [])
            if sources:
                episode_urls[str(episode["number"])] = sources[0]["embed_url"]
        self.assertEqual(parse_qs(urlparse(episode_urls["1"]).query).get("episode"), ["1"])
        self.assertEqual(parse_qs(urlparse(episode_urls["2"]).query).get("episode"), ["2"])
        self.assertNotEqual(episode_urls["1"], episode_urls["2"])

    def test_legacy_kodik_serial_player_urls_are_episode_specific(self):
        html = """
        <div class="serial-seasons-box">
          <select>
            <option value="2" data-serial-id="76688" data-serial-hash="46dc" selected>2 сезон</option>
          </select>
        </div>
        <div class="serial-series-box">
          <select>
            <option value="1" data-title="1 серия" selected>1 серия</option>
            <option value="2" data-title="2 серия">2 серия</option>
          </select>
        </div>
        """

        urls = scrape_yummyanime.parse_kodik_serial_episode_urls(
            "https://kodikplayer.com/serial/76549/oldhash/720p",
            html,
        )

        self.assertEqual([item["episode_number"] for item in urls], ["1", "2"])
        self.assertEqual(parse_qs(urlparse(urls[0]["embed_url"]).query), {"season": ["2"], "episode": ["1"]})
        self.assertEqual(parse_qs(urlparse(urls[1]["embed_url"]).query), {"season": ["2"], "episode": ["2"]})
        self.assertIn("/serial/76688/46dc/720p", urls[0]["embed_url"])

    def test_legacy_multi_episode_fallback_does_not_fan_out_single_video(self):
        with patch.object(scrape_yummyanime, "fetch_text", side_effect=RuntimeError("offline")):
            providers = scrape_yummyanime.expand_legacy_provider_urls(
                {"provider_title": "Kodik"},
                "https://kodikplayer.com/video/104168/hash/720p",
                "https://yummyanime.tv/title.html",
                12,
            )

        self.assertEqual(providers, [{"episode_number": "1", "embed_url": "https://kodikplayer.com/video/104168/hash/720p"}])

    def test_legacy_kodik_prefers_matching_title_season_when_default_count_differs(self):
        default_html = """
        <div class="serial-seasons-box">
          <select>
            <option value="1" data-serial-id="33456" data-serial-hash="dota" selected>1 сезон</option>
            <option value="2" data-serial-id="33456" data-serial-hash="dota">2 сезон</option>
          </select>
        </div>
        <div class="serial-series-box">
          <select><option value="1" data-title="1 серия">1 серия</option></select>
        </div>
        """
        season_two_html = """
        <div class="serial-seasons-box">
          <select>
            <option value="1" data-serial-id="33456" data-serial-hash="dota">1 сезон</option>
            <option value="2" data-serial-id="33456" data-serial-hash="dota" selected>2 сезон</option>
          </select>
        </div>
        <div class="serial-series-box">
          <select>
            <option value="1" data-title="1 серия">1 серия</option>
            <option value="2" data-title="2 серия">2 серия</option>
          </select>
        </div>
        """

        with patch.object(scrape_yummyanime, "fetch_text", side_effect=[default_html, season_two_html]):
            providers = scrape_yummyanime.expand_legacy_provider_urls(
                {"provider_title": "Kodik"},
                "https://kodikplayer.com/serial/33456/dota/720p",
                "https://yummyanime.tv/dota-2.html",
                2,
                preferred_season="2",
            )

        self.assertEqual([item["episode_number"] for item in providers], ["1", "2"])
        self.assertEqual(parse_qs(urlparse(providers[0]["embed_url"]).query), {"season": ["2"], "episode": ["1"]})

    def test_duplicate_sources_are_exposed_as_canonical_titles(self):
        items = server.get_anime_list()
        merged = [
            item
            for item in items
            if {"animego", "yummyanime"}.issubset(set(item.get("sources") or []))
        ]
        self.assertGreaterEqual(len(merged), 10)

        item = merged[0]
        self.assertEqual(item["source"], "animego")
        self.assertEqual(item["source_variant_count"], 2)

        detail = server.get_anime_detail(item["id"])
        self.assertEqual(detail["id"], item["id"])
        self.assertEqual(detail["source"], "animego")
        self.assertIn("animego", detail["sources"])
        self.assertIn("yummyanime", detail["sources"])

        yummy_variant = next(variant for variant in detail["source_variants"] if variant["source"] == "yummyanime")
        same_detail = server.get_anime_detail(yummy_variant["id"])
        self.assertEqual(same_detail["id"], item["id"])
        self.assertEqual(same_detail["source"], "animego")

    def test_canonical_merge_rejects_conflicting_subtitles_and_transitive_bridge(self):
        def item(anime_id, source, source_id, title, subtitle, year="2026"):
            return {
                "id": anime_id,
                "source": source,
                "source_id": source_id,
                "title": title,
                "subtitle": subtitle,
                "year": year,
                "source_count": 1,
                "available_episode_count": 1,
                "episode_count": 1,
                "genres": [],
                "search_fields": [],
            }

        conflicting = server.canonicalize_items(
            [
                item(1, "animego", "1", "Shared Long Title", "Original Alpha"),
                item(10000001, "yummyanime", "1", "Shared Long Title", "Original Beta"),
            ]
        )
        self.assertEqual(len(conflicting), 2)

        coiling_dragon = server.canonicalize_items(
            [
                item(10008976, "yummyanime", "8976", "Извивающийся дракон", "Panlong"),
                {
                    **item(
                        20027850,
                        "yummyanime",
                        "yummyani:27850",
                        "Извивающийся дракон",
                        "Coiling Dragon",
                    ),
                    "_canonical_aliases": ["Coiling Dragon", "Panlong"],
                },
            ]
        )
        self.assertEqual(len(coiling_dragon), 1)
        self.assertEqual(
            set(coiling_dragon[0]["source_member_ids"]),
            {10008976, 20027850},
        )

        bridged = server.canonicalize_items(
            [
                item(2, "animego", "2", "Shared Bridge Title", "Shared Original"),
                item(10000002, "yummyanime", "2", "Localized Bridge Title", "Shared Original"),
                item(20000002, "yummyanime", "yummyani:2", "Shared Bridge Title", None),
            ]
        )
        self.assertEqual(sorted(group["source_variant_count"] for group in bridged), [1, 2])
        self.assertEqual(
            next(group for group in bridged if group["source_variant_count"] == 2)["source_member_ids"],
            [2, 10000002],
        )

        shared_romaji = "Tsuihou sareta Tensei Juukishi wa Game Chishiki de Musou suru"
        heavy_knight = [
            item(3429, "animego", "3429", "Изгнанный тяжёлый рыцарь: знания игры", shared_romaji),
            item(10008486, "yummyanime", "8486", "Изгнанный тяжёлый рыцарь", shared_romaji),
            {
                **item(
                    20014472,
                    "yummyanime",
                    "yummyani:14472",
                    "Изгнанный тяжёлый рыцарь: знания игры",
                    "The Exiled Heavy Knight Knows How to Game the System",
                ),
                "_canonical_aliases": [shared_romaji],
            },
        ]
        three_way = server.canonicalize_items(heavy_knight)
        self.assertEqual(len(three_way), 1)
        self.assertEqual(set(three_way[0]["source_member_ids"]), {3429, 10008486, 20014472})

        different_year = [dict(value) for value in heavy_knight[:2]]
        different_year[1]["year"] = "2025"
        self.assertEqual(len(server.canonicalize_items(different_year)), 2)

        same_namespace = [dict(value) for value in heavy_knight[:2]]
        same_namespace[1]["source"] = "animego"
        self.assertEqual(len(server.canonicalize_items(same_namespace)), 2)

    def test_canonical_merge_uses_exact_semicolon_delimited_other_titles_and_variant_slugs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            con = scrape_animego.init_db(db_path)
            con.row_factory = sqlite3.Row
            try:
                now = server.now_iso()
                rows = (
                    (
                        3623,
                        "animego",
                        "3623",
                        "Адский режим: Геймер, который любит спидран, становится бесподобным 2",
                        "Hell Mode: Yarikomizuki no Gamer wa Hai Settei no Isekai de Musou suru 2nd Season",
                    ),
                    (
                        20027421,
                        "yummyanime",
                        "yummyani:27421",
                        "Адский уровень: Хардкорный геймер в другом мире 2",
                        "Hell Mode: The Hardcore Gamer Dominates in Another World Season 2",
                    ),
                )
                for anime_id, source, source_id, title, subtitle in rows:
                    con.execute(
                        """
                        insert into anime(id, slug, title, subtitle, url, source, source_id, year, scraped_at)
                        values (?, ?, ?, ?, ?, ?, ?, '2026', ?)
                        """,
                        (anime_id, f"source-{anime_id}", title, subtitle, f"https://example.test/{anime_id}", source, source_id, now),
                    )
                    episode_id = anime_id * 1000 + 1
                    con.execute(
                        "insert into episodes(id, anime_id, number, has_video, scraped_at) values (?, ?, '1', 1, ?)",
                        (episode_id, anime_id, now),
                    )
                    con.execute(
                        """
                        insert into video_sources(
                            anime_id, episode_id, provider_id, translation_id,
                            embed_url, embed_url_redacted, scraped_at
                        ) values (?, ?, ?, 1, ?, ?, ?)
                        """,
                        (
                            anime_id,
                            episode_id,
                            f"provider-{anime_id}",
                            f"https://example.test/embed/{anime_id}",
                            f"https://example.test/embed/{anime_id}",
                            now,
                        ),
                    )
                exact_alias = rows[0][3]
                con.execute(
                    "insert into anime_fields(anime_id, label, value) values (?, 'Другие названия', ?)",
                    (20027421, f"{exact_alias}; Другое название"),
                )
                con.commit()

                aliases = server.load_canonical_match_aliases(con)[20027421]
                self.assertIn(exact_alias, aliases)
                self.assertNotIn("который любит спидран", aliases)
            finally:
                con.close()
            server.reset_database_initialization(db_path)
            server.invalidate_catalog_cache(db_path)

            items = server.get_anime_list(db_path)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["id"], 3623)
            self.assertEqual(set(items[0]["source_member_ids"]), {3623, 20027421})

            old_yummy_slug = server.canonical_slug_for_item(
                {"id": 20027421, "title": rows[1][3], "subtitle": rows[1][4]}
            )
            self.assertEqual(server.get_anime_detail(old_yummy_slug, db_path)["id"], 3623)

    def test_canonical_union_builds_component_metadata_once_per_item(self):
        items = []
        for index in range(300):
            title = f"Canonical performance pair {index}"
            subtitle = f"Canonical performance original {index}"
            for anime_id, source in ((index + 1, "animego"), (10_000_001 + index, "yummyanime")):
                items.append(
                    {
                        "id": anime_id,
                        "source": source,
                        "source_id": str(index + 1),
                        "title": title,
                        "subtitle": subtitle,
                        "year": "2026",
                        "source_count": 1,
                        "available_episode_count": 1,
                        "episode_count": 1,
                        "genres": [],
                        "search_fields": [],
                    }
                )

        with patch.object(
            server,
            "canonical_component_metadata",
            wraps=server.canonical_component_metadata,
        ) as metadata:
            groups = server.canonicalize_items(items)

        self.assertEqual(len(groups), 300)
        self.assertTrue(all(group["source_variant_count"] == 2 for group in groups))
        self.assertEqual(metadata.call_count, len(items))

    def test_state_patch_is_strict_and_preserves_concurrent_independent_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            anime_id = self.seed_watchable_title(db_path, anime_id=301)
            user_id = self.create_google_user(db_path, "concurrent-user", "concurrent@example.com")

            for invalid in (
                {"is_favorite": "false"},
                {"watched": 1},
                {"progress_episode_number": -1},
                {"unknown": True},
                {},
            ):
                with self.subTest(invalid=invalid):
                    with self.assertRaises(ValueError):
                        server.update_user_state(anime_id, invalid, db_path, user_id)

            barrier = threading.Barrier(3)
            errors = []

            def update(patch):
                try:
                    barrier.wait()
                    server.update_user_state(anime_id, patch, db_path, user_id)
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            threads = [
                threading.Thread(target=update, args=({"is_favorite": True},)),
                threading.Thread(target=update, args=({"watched": True},)),
            ]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=5)

            self.assertEqual(errors, [])
            detail = server.get_anime_detail(anime_id, db_path, user_id)
            self.assertTrue(detail["is_favorite"])
            self.assertTrue(detail["watched"])

    def test_watch_event_rejects_mismatched_episode_source_and_progress(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            anime_id = self.seed_watchable_title(db_path, anime_id=302, episode_count=2)
            user_id = self.create_google_user(db_path, "watch-validation", "watch@example.com")
            detail = server.get_anime_detail(anime_id, db_path, user_id)

            source_mismatch = self.watch_payload(detail, episode_index=0)
            source_mismatch["video_source_id"] = detail["sources_by_episode"][detail["episodes"][1]["id"]][0]["id"]
            with self.assertRaisesRegex(ValueError, "invalid for this episode"):
                server.record_watch_event(source_mismatch, db_path, user_id)

            progress_mismatch = self.watch_payload(detail, episode_index=0)
            progress_mismatch["progress_episode_number"] = 2
            with self.assertRaisesRegex(ValueError, "does not match episode_id"):
                server.record_watch_event(progress_mismatch, db_path, user_id)

    def test_connect_enforces_foreign_keys_and_purges_watch_orphans_on_startup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            anime_id = self.seed_watchable_title(db_path, anime_id=303)
            server.prepare_database(db_path)
            raw = sqlite3.connect(db_path)
            raw.execute("pragma foreign_keys=off")
            timestamp = server.now_iso()
            raw.execute(
                """
                insert into user_watch_events(
                    user_id, anime_id, client_session_id, event_type, event_at,
                    confidence, metadata_json, created_at
                ) values (999999, ?, 'orphan', 'player_loaded', ?, 0.1, '{}', ?)
                """,
                (anime_id, timestamp, timestamp),
            )
            raw.commit()
            raw.close()

            server.reset_database_initialization(db_path)
            con = server.connect(db_path)
            try:
                self.assertEqual(con.execute("pragma foreign_keys").fetchone()[0], 1)
                self.assertEqual(con.execute("select count(*) from user_watch_events").fetchone()[0], 0)
                self.assertEqual(con.execute("pragma foreign_key_check").fetchall(), [])
            finally:
                con.close()

    def test_startup_nulls_optional_orphan_refs_without_losing_watch_history(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            anime_id = self.seed_watchable_title(db_path, anime_id=306)
            user_id = self.create_google_user(db_path, "optional-orphans", "orphan@example.com")
            con = server.connect(db_path)
            try:
                episode_id = con.execute(
                    "select id from episodes where anime_id = ? limit 1", (anime_id,)
                ).fetchone()[0]
            finally:
                con.close()

            timestamp = server.now_iso()
            raw = sqlite3.connect(db_path)
            raw.execute("pragma foreign_keys=off")
            raw.execute(
                """
                insert into user_watch_events(
                    user_id, anime_id, episode_id, video_source_id, source_anime_id,
                    client_session_id, event_type, event_at, confidence,
                    metadata_json, created_at
                ) values (?, ?, 999991, 999992, 999993, 'orphan-refs',
                          'player_loaded', ?, 0.1, '{}', ?)
                """,
                (user_id, anime_id, timestamp, timestamp),
            )
            raw.execute(
                """
                insert into user_episode_state(
                    user_id, anime_id, episode_id, video_source_id, source_anime_id,
                    first_seen_at, last_seen_at, last_event_type, last_confidence,
                    updated_at
                ) values (?, ?, ?, 999992, 999993, ?, ?, 'player_loaded', 0.1, ?)
                """,
                (user_id, anime_id, episode_id, timestamp, timestamp, timestamp),
            )
            raw.commit()
            raw.close()

            server.reset_database_initialization(db_path)
            con = server.connect(db_path)
            try:
                event = con.execute(
                    "select episode_id, video_source_id, source_anime_id from user_watch_events "
                    "where client_session_id = 'orphan-refs'"
                ).fetchone()
                state = con.execute(
                    "select video_source_id, source_anime_id from user_episode_state "
                    "where user_id = ? and anime_id = ? and episode_id = ?",
                    (user_id, anime_id, episode_id),
                ).fetchone()
                self.assertIsNotNone(event)
                self.assertEqual(tuple(event), (None, None, None))
                self.assertIsNotNone(state)
                self.assertEqual(tuple(state), (None, None))
                self.assertEqual(con.execute("pragma foreign_key_check").fetchall(), [])
            finally:
                con.close()

    def test_connect_fast_path_initializes_once_and_detects_in_place_ddl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            server.reset_database_initialization(db_path)
            with patch.object(
                server,
                "initialize_database",
                wraps=server.initialize_database,
            ) as initialize:
                first = server.connect(db_path)
                first.close()
                second = server.connect(db_path)
                second.close()
                self.assertEqual(initialize.call_count, 1)

                raw = sqlite3.connect(db_path)
                raw.execute("drop table login_handoffs")
                raw.commit()
                raw.close()

                third = server.connect(db_path)
                try:
                    self.assertIsNotNone(
                        third.execute(
                            "select 1 from sqlite_master where type = 'table' "
                            "and name = 'login_handoffs'"
                        ).fetchone()
                    )
                finally:
                    third.close()
                self.assertEqual(initialize.call_count, 2)

    def test_connect_detects_atomic_database_replacement(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "animego.sqlite"
            replacement = Path(tmpdir) / "replacement.sqlite"
            server.prepare_database(db_path)
            old_identity = db_path.stat().st_ino
            scrape_animego.init_db(replacement).close()
            os.replace(replacement, db_path)
            self.assertNotEqual(db_path.stat().st_ino, old_identity)

            con = server.connect(db_path)
            try:
                self.assertIsNotNone(
                    con.execute(
                        "select 1 from sqlite_master where type = 'table' and name = 'sessions'"
                    ).fetchone()
                )
                self.assertEqual(con.execute("pragma foreign_keys").fetchone()[0], 1)
            finally:
                con.close()

    def test_catalog_cache_observes_external_catalog_writes_without_manual_invalidation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            self.seed_watchable_title(db_path, anime_id=304, title="Before Migration")
            self.assertEqual(len(server.get_anime_list(db_path)), 1)

            con = sqlite3.connect(db_path)
            timestamp = server.now_iso()
            con.execute(
                """
                insert into anime(id, slug, title, url, source, source_id, scraped_at)
                values (305, 'after-migration', 'After Migration', 'https://example.test/305', 'animego', '305', ?)
                """,
                (timestamp,),
            )
            con.execute(
                "insert into episodes(id, anime_id, number, has_video, scraped_at) values (305001, 305, '1', 1, ?)",
                (timestamp,),
            )
            con.execute(
                """
                insert into video_sources(anime_id, episode_id, provider_id, embed_url, scraped_at)
                values (305, 305001, 'fixture', 'https://example.test/embed/305', ?)
                """,
                (timestamp,),
            )
            con.commit()
            con.close()

            self.assertEqual({item["title"] for item in server.get_anime_list(db_path)}, {"Before Migration", "After Migration"})

    def test_readiness_rejects_missing_and_corrupt_databases(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "missing.sqlite"
            self.assertFalse(server.database_is_ready(missing))

            corrupt = Path(tmpdir) / "corrupt.sqlite"
            corrupt.write_bytes(b"not a sqlite database")
            self.assertFalse(server.database_is_ready(corrupt))
            status, _, body = self.request_test_server(str(corrupt), "GET", "/api/health")
            self.assertEqual(status, 503)
            self.assertFalse(json.loads(body)["ok"])

    def test_readiness_checks_foreign_keys_and_caches_expensive_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            healthy = Path(tmpdir) / "healthy.sqlite"
            server.prepare_database(healthy)
            server.reset_database_initialization(healthy)
            with patch.object(
                server,
                "check_database_readiness",
                wraps=server.check_database_readiness,
            ) as check:
                self.assertTrue(server.database_is_ready(healthy))
                self.assertTrue(server.database_is_ready(healthy))
                self.assertEqual(check.call_count, 1)

            broken = Path(tmpdir) / "broken.sqlite"
            shutil.copy(healthy, broken)
            raw = sqlite3.connect(broken)
            raw.execute("pragma foreign_keys=off")
            raw.execute(
                """
                insert into sessions(token_hash, user_id, created_at, expires_at)
                values ('orphan-session', 999999, ?, ?)
                """,
                (server.now_iso(), "2099-01-01T00:00:00+00:00"),
            )
            raw.commit()
            raw.close()
            self.assertFalse(server.database_is_ready(broken))

    def test_readiness_cache_detects_atomic_database_replacement(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "animego.sqlite"
            replacement = Path(tmpdir) / "replacement.sqlite"
            server.prepare_database(db_path)

            self.assertTrue(server.database_is_ready(db_path))
            original_identity = server.readiness_file_identity(db_path)
            replacement.write_bytes(b"not a sqlite database")
            os.replace(replacement, db_path)

            self.assertNotEqual(server.readiness_file_identity(db_path), original_identity)
            self.assertFalse(server.database_is_ready(db_path))

    def test_readiness_cache_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with server.READINESS_CACHE_LOCK:
                server.READINESS_CACHE.clear()
            for index in range(server.READINESS_CACHE_MAX_ENTRIES + 5):
                self.assertFalse(
                    server.database_is_ready(Path(tmpdir) / f"missing-{index}.sqlite")
                )
            with server.READINESS_CACHE_LOCK:
                self.assertLessEqual(
                    len(server.READINESS_CACHE),
                    server.READINESS_CACHE_MAX_ENTRIES,
                )

    def test_accept_encoding_respects_explicit_zero_quality(self):
        self.assertFalse(server.accepts_content_encoding("gzip;q=0, br", "gzip"))
        self.assertFalse(server.accepts_content_encoding("*;q=1, gzip;q=0", "gzip"))
        self.assertTrue(server.accepts_content_encoding("br, gzip;q=0.5", "gzip"))

    def test_search_query_work_is_bounded(self):
        query = " ".join(f"token{index}" for index in range(1000))
        parsed = server.search_query_info(query)
        self.assertLessEqual(len(parsed["tokens"]), server.MAX_SEARCH_QUERY_TOKENS)
        self.assertLessEqual(len(parsed["text"]), server.MAX_SEARCH_QUERY_CHARS)

    def test_yummyani_namespace_allows_modern_and_legacy_title_merge(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            scraped_at = server.now_iso()
            con = scrape_animego.init_db(db_path)
            try:
                for anime_id, source, source_id in (
                    (3001, "animego", "3001"),
                    (10003001, "yummyanime", "3001"),
                    (20003001, "yummyanime", "yummyani:3001"),
                ):
                    year = None if anime_id >= 20000000 else "2026"
                    title = "Namespace Merge сезон" if anime_id == 10003001 else "Namespace Merge"
                    subtitle = "Namespace Merge Romaji"
                    con.execute(
                        """
                        insert into anime(id, slug, title, subtitle, url, source, source_id, year, scraped_at)
                        values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            anime_id,
                            f"namespace-merge-{anime_id}",
                            title,
                            subtitle,
                            f"https://example.test/{anime_id}",
                            source,
                            source_id,
                            year,
                            scraped_at,
                        ),
                    )
                    con.execute(
                        """
                        insert into episodes(id, anime_id, number, has_video, scraped_at)
                        values (?, ?, '1', 1, ?)
                        """,
                        (anime_id * 1000 + 1, anime_id, scraped_at),
                    )
                    con.execute(
                        """
                        insert into video_sources(
                            anime_id, episode_id, provider_id, provider_title,
                            translation_id, translation_title, embed_url, embed_url_redacted,
                            embed_host, scraped_at
                        )
                        values (?, ?, ?, 'Kodik', ?, 'Dream Cast', ?, ?, 'kodikplayer.com', ?)
                        """,
                        (
                            anime_id,
                            anime_id * 1000 + 1,
                            f"provider-{anime_id}",
                            anime_id,
                            f"https://kodikplayer.com/serial/{anime_id}/hash/720p?season=1&episode=1",
                            f"https://kodikplayer.com/serial/{anime_id}/<redacted>/720p",
                            scraped_at,
                        ),
                    )
                con.commit()
            finally:
                con.close()
            server.invalidate_catalog_cache(db_path)

            matches = [item for item in server.get_anime_list(db_path) if item["title"] == "Namespace Merge"]
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0]["source"], "animego")
            self.assertEqual(matches[0]["source_variant_count"], 3)
            self.assertEqual(set(matches[0]["source_member_ids"]), {3001, 10003001, 20003001})

    def test_frieren_details_have_full_episode_video_coverage(self):
        expected = {
            2430: 28,
            2911: 10,
        }
        for anime_id, episode_count in expected.items():
            with self.subTest(anime_id=anime_id):
                detail = server.get_anime_detail(anime_id)
                self.assertEqual(len(detail["episodes"]), episode_count)
                self.assertEqual(detail["available_episode_count"], episode_count)
                self.assertTrue(all(episode["source_count"] > 0 for episode in detail["episodes"]))

    def test_subtitle_matched_duplicate_sources_are_merged(self):
        items = server.get_anime_list()
        moon = [
            item
            for item in items
            if "Лунное путешествие приведёт к новому миру 2" in item["title"]
        ]

        self.assertEqual(len(moon), 1)
        self.assertEqual(moon[0]["source"], "animego")
        self.assertEqual(
            {variant["source"] for variant in moon[0]["source_variants"]},
            {"animego", "yummyanime"},
        )
        self.assertEqual(set(moon[0]["source_member_ids"]), {2463, 10001210})


if __name__ == "__main__":
    unittest.main()
