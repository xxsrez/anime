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

    def seed_watchable_title(self, db_path, anime_id=1, title="Smoke Title"):
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
                values (?, ?, 'kodik', 'Kodik', 1, 'Dream Cast', ?, ?, 'kodikplayer.com', ?)
                """,
                (
                    anime_id,
                    anime_id * 1000 + 1,
                    f"https://kodikplayer.com/serial/{anime_id}/hash/720p?season=1&episode=1",
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
                )
                con.commit()
            finally:
                con.close()
            server.invalidate_catalog_cache(db_path)

            item = next(entry for entry in server.get_anime_list(db_path) if entry["id"] == anime_id)
            self.assertEqual(item["recent_update_summary"]["badge"], "+1 серия")
            self.assertEqual(item["recent_updates"][0]["event_type"], "new_episode")

            detail = server.get_anime_detail(anime_id, db_path)
            self.assertEqual(detail["recent_update_summary"]["label"], "Добавлено 1 серия")
            self.assertEqual(detail["recent_updates"][0]["episode_number"], "1")

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
                    100,
                )
        scored = [item for item in items if item.get("aggregate_score")]
        self.assertGreaterEqual(len(scored), 200)
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

    def test_user_state_update_requires_real_user(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)
            anime_id = server.get_anime_list(db_path)[0]["id"]

            with self.assertRaisesRegex(ValueError, "user_id is required"):
                server.update_user_state(anime_id, {"is_favorite": True}, db_path)
            with self.assertRaisesRegex(ValueError, "user_id does not exist"):
                server.update_user_state(anime_id, {"is_favorite": True}, db_path, 999999)

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
            anime_id = server.get_anime_list(db_path, user_id=admin_id)[0]["id"]
            server.update_user_state(
                anime_id,
                {"is_favorite": True, "progress_episode_number": 4},
                db_path,
                admin_id,
            )
            server.update_user_state(anime_id, {"watched": True}, db_path, other_id)

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
            users = {item["email"]: item for item in payload["users"]}
            self.assertEqual(set(users), {"one@example.com", "two@example.com"})
            self.assertTrue(users["one@example.com"]["is_admin"])
            self.assertFalse(users["two@example.com"]["is_admin"])
            self.assertEqual(users["one@example.com"]["favorite_titles"], 1)
            self.assertEqual(users["two@example.com"]["watched_titles"], 1)
            self.assertTrue(payload["top_titles"])

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
            auth = {
                "user": {"id": 10, "email": "one@example.com", "name": "One", "picture_url": None},
                "token": "session-token",
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
            self.assertIn(f"{server.SESSION_COOKIE_NAME}=session-token", headers["Set-Cookie"])
            self.assertIn(b"waitForSession", response_body)
            self.assertIn(b'fetch("/api/me"', response_body)
            self.assertIn(b'credentials: "same-origin"', response_body)
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
            auth = {
                "user": {"id": 10, "email": "one@example.com", "name": "One", "picture_url": None},
                "token": "session-token",
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
            self.assertIn(f"{server.SESSION_COOKIE_NAME}=session-token", headers["Set-Cookie"])
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

    def test_safe_next_path_rejects_external_and_relative_targets(self):
        self.assertEqual(server.safe_next_path("/wanted?tab=favorites#details"), "/wanted?tab=favorites#details")
        self.assertEqual(server.safe_next_path("relative/path"), "/")
        self.assertEqual(server.safe_next_path("//evil.example/path"), "/")
        self.assertEqual(server.safe_next_path("https://evil.example/path"), "/")

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
            self.assertEqual(payload["profile"]["mode"], "popular")
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
        self.assertIn('id="one-tap-anchor"', html)
        self.assertIn('id="google-button"', html)
        self.assertIn("google.accounts.id.prompt", js)
        self.assertIn("function authError()", js)
        self.assertIn('get("auth_error")', js)
        self.assertIn("api/auth/config?next=", js)
        self.assertIn("auto_select: true", js)
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
        self.assertIn("maybeShowOneTap(google, Boolean(redirectError))", js)
        self.assertIn("renderUnavailableGoogleButton", js)
        self.assertIn("google-fallback-button", js)
        self.assertIn("Ошибка конфигурации деплоймента", js)
        self.assertIn("/api/client-errors", reporter_js)
        self.assertIn("window.addEventListener(\"error\"", reporter_js)
        self.assertIn("window.addEventListener(\"unhandledrejection\"", reporter_js)

    def test_app_boot_parallelizes_initial_api_and_defers_recommendations(self):
        js = Path(server.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        boot = js[js.index("async function boot()"):js.index("boot().catch")]
        self.assertIn("Promise.all", boot)
        self.assertIn('api("/api/me")', boot)
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
            self.assertEqual(headers["Referrer-Policy"], "no-referrer-when-downgrade")

    def test_view_mode_tabs_use_compact_accessible_labels(self):
        html = Path(server.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn('aria-label="Режим каталога"', html)
        self.assertIn('class="view-tab-icon"', html)
        self.assertIn('class="view-tab-label">Избр.</span>', html)
        self.assertIn('aria-label="Избранное"', html)
        self.assertIn('class="view-tab-label">Смотрю</span>', html)
        self.assertIn('aria-pressed="true"', html)

    def test_admin_page_has_dashboard_assets(self):
        html = Path(server.STATIC_DIR / "admin.html").read_text(encoding="utf-8")
        js = Path(server.STATIC_DIR / "admin.js").read_text(encoding="utf-8")
        self.assertIn('body class="admin-page"', html)
        self.assertIn('id="admin-summary"', html)
        self.assertIn('id="admin-users"', html)
        self.assertIn('id="admin-top-titles"', html)
        self.assertIn('src="/static/admin.js"', html)
        self.assertIn("/api/admin/users", js)
        self.assertIn("is_admin", js)

    def test_right_pane_deep_links_are_supported(self):
        js = Path(server.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn("readLinkState", js)
        self.assertIn("syncUrlFromDetail", js)
        self.assertIn("window.history", js)

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
        self.assertNotIn("selectFirst", save_user_state)

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
                    subtitle = "Namespace Merge English" if anime_id >= 20000000 else "Namespace Merge Romaji"
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
