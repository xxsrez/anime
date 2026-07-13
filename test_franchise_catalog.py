#!/usr/bin/env python3
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

import franchise_catalog
import scrape_animego
import server


class FranchiseCatalogTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "animego.sqlite"
        con = scrape_animego.init_db(self.db_path)
        try:
            self.add_title(con, 2540, "animego", "2540", "О моём перерождении в слизь 3", "Вышел")
            self.add_title(
                con,
                10002222,
                "yummyanime",
                "2222",
                "О моём перерождении в слизь 3",
                "Вышел",
            )
            self.add_title(con, 3220, "animego", "3220", "О моём перерождении в слизь 4", "Онгоинг")
            con.commit()
        finally:
            con.close()
        server.prepare_database(self.db_path)
        server.invalidate_catalog_cache(self.db_path)

    def tearDown(self):
        server.invalidate_catalog_cache(self.db_path)
        server.reset_database_initialization(self.db_path)
        franchise_catalog.reset_cache()
        self.tmpdir.cleanup()

    @staticmethod
    def add_title(con, anime_id, source, source_id, title, status):
        scraped_at = "2026-07-13T00:00:00+00:00"
        con.execute(
            """
            insert into anime(
                id, slug, title, subtitle, url, cover_url, kind, year, status,
                episodes_text, scraped_at, source, source_id
            ) values (?, ?, ?, ?, ?, ?, 'Сериал', ?, ?, '1', ?, ?, ?)
            """,
            (
                anime_id,
                f"slime-{anime_id}",
                title,
                "Tensei Shitara Slime Datta Ken",
                f"https://example.test/anime/{anime_id}",
                f"https://example.test/posters/{anime_id}.jpg",
                "2026" if "4" in title else "2024",
                status,
                scraped_at,
                source,
                source_id,
            ),
        )
        episode_id = anime_id * 100 + 1
        con.execute(
            "insert into episodes(id, anime_id, number, has_video, scraped_at) values (?, ?, '1', 1, ?)",
            (episode_id, anime_id, scraped_at),
        )
        con.execute(
            """
            insert into video_sources(
                anime_id, episode_id, provider_id, provider_title,
                translation_id, translation_title, embed_host, embed_url,
                embed_url_redacted, scraped_at
            ) values (?, ?, 'kodik', 'Kodik', 1, 'Dream Cast',
                      'kodikplayer.com', ?, ?, ?)
            """,
            (
                anime_id,
                episode_id,
                f"https://kodikplayer.com/serial/{anime_id}/fixture/720p?episode=1",
                f"https://kodikplayer.com/serial/{anime_id}/<redacted>/720p",
                scraped_at,
            ),
        )

    def create_user_session(self):
        con = server.connect(self.db_path)
        try:
            user = server.upsert_google_user(
                con,
                {
                    "sub": "franchise-test-user",
                    "email": "franchise@example.com",
                    "email_verified": True,
                    "name": "Franchise Test",
                    "picture": None,
                },
            )
            token, _ = server.create_session(con, user["id"])
            con.commit()
            return user["id"], token
        finally:
            con.close()

    def request(self, path, token=None):
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.AnimeHandler)
        httpd.db_path = self.db_path
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            headers = {"Cookie": f"{server.SESSION_COOKIE_NAME}={token}"} if token else {}
            con = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
            con.request("GET", path, headers=headers)
            response = con.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_curated_definition_has_distinct_release_and_watch_orders(self):
        definition = franchise_catalog.get_definition("tensei-slime")

        self.assertEqual(len(definition["entries"]), 15)
        self.assertEqual(
            [entry["release_order"] for entry in definition["entries"]],
            list(range(1, 16)),
        )
        entries = {entry["key"]: entry for entry in definition["entries"]}
        self.assertLess(entries["oad"]["watch_order"], entries["veldora-diary-1"]["watch_order"])
        self.assertEqual(entries["season-1"]["match"]["anidb"], [13871])

    def test_definition_validation_rejects_malformed_source_and_dates(self):
        raw = json.loads((Path("content/franchises/tensei-slime.json")).read_text())
        malformed_source = dict(raw)
        malformed_source["source"] = "official site"
        with self.assertRaisesRegex(franchise_catalog.FranchiseDataError, "source must be an object"):
            franchise_catalog.validate_definition(malformed_source)

        malformed_date = json.loads(json.dumps(raw))
        malformed_date["entries"][0]["release_date"] = "2018/10/02"
        with self.assertRaisesRegex(franchise_catalog.FranchiseDataError, "must use YYYY-MM-DD"):
            franchise_catalog.validate_definition(malformed_date)

        reversed_dates = json.loads(json.dumps(raw))
        reversed_dates["entries"][0]["release_end_date"] = "2018-01-01"
        with self.assertRaisesRegex(franchise_catalog.FranchiseDataError, "cannot precede"):
            franchise_catalog.validate_definition(reversed_dates)

    def test_compact_summary_supports_year_only_entries(self):
        summary = franchise_catalog.compact_summary(
            {
                "slug": "year-only",
                "title": "Year only",
                "entries": [
                    {"release_year": 2001, "status": "finished", "story_role": "main"},
                    {"release_year": 2003, "status": "finished", "story_role": "optional"},
                ],
            }
        )

        self.assertEqual(summary["year_range"], "2001 — 2003")

    def test_franchise_detail_matches_canonical_variants_and_keeps_missing_release(self):
        payload = server.get_franchise_detail(
            "tensei-slime",
            self.db_path,
            current_anime_ref="10002222",
        )
        entries = {entry["id"]: entry for entry in payload["entries"]}

        self.assertEqual(payload["entry_count"], 15)
        self.assertEqual(entries["season-3"]["catalog_item"]["id"], 2540)
        self.assertEqual(len(entries["season-3"]["catalog_item"]["source_variants"]), 1)
        self.assertTrue(entries["season-3"]["is_current"])
        self.assertEqual(entries["season-1"]["availability"], "not_in_catalog")
        self.assertNotIn("catalog_item", entries["season-1"])
        self.assertEqual(entries["season-4"]["status"], "releasing")
        self.assertEqual(entries["visions-of-coleus"]["kind"], "OVA")

    def test_secondary_source_id_can_match_when_canonical_primary_is_animego(self):
        items = server.get_anime_list(self.db_path)
        source_groups = server.catalog_groups_by_source(items)

        candidates = server.franchise_entry_catalog_candidates(
            {"match": {"sources": ["yummyanime:2222"]}},
            source_groups,
            {},
        )

        self.assertEqual([item["id"] for item in candidates], [2540])

    def test_ambiguous_catalog_match_is_rejected_instead_of_ranked(self):
        with self.assertRaisesRegex(
            franchise_catalog.FranchiseDataError,
            "matches multiple canonical catalog groups",
        ):
            server.select_franchise_catalog_candidate(
                [{"id": 1}, {"id": 2}],
                {"key": "ambiguous-release"},
            )

    def test_title_detail_exposes_same_franchise_from_secondary_source(self):
        detail = server.get_anime_detail(10002222, self.db_path)

        self.assertEqual(detail["id"], 2540)
        self.assertEqual(detail["franchise"]["slug"], "tensei-slime")
        self.assertEqual(detail["franchise"]["current_entry_id"], "season-3")

    def test_franchise_api_auth_not_found_and_deep_link(self):
        status, _, _ = self.request("/api/franchises/tensei-slime")
        self.assertEqual(status, 401)

        _, token = self.create_user_session()
        status, _, body = self.request(
            "/api/franchises/tensei-slime?current=10002222",
            token,
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        current = next(entry for entry in payload["entries"] if entry["id"] == "season-3")
        self.assertTrue(current["is_current"])

        status, _, _ = self.request("/api/franchises/not-found", token)
        self.assertEqual(status, 404)
        status, _, body = self.request("/franchises/tensei-slime", token)
        self.assertEqual(status, 200)
        self.assertIn(b'id="franchise-detail-view"', body)


if __name__ == "__main__":
    unittest.main()
