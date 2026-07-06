#!/usr/bin/env python3
from pathlib import Path
import shutil
import tempfile
import unittest

import server


class LocalAppTest(unittest.TestCase):
    def test_catalog_has_scraped_titles(self):
        items = server.get_anime_list()
        self.assertGreaterEqual(len(items), 10)
        self.assertTrue(all(item["source_count"] > 0 for item in items))
        self.assertTrue(all(item["available_episode_count"] > 0 for item in items))
        slugs = [item["slug"] for item in items]
        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertTrue(all(item["internal_id"] == item["slug"] for item in items))
        self.assertTrue(all("-" in item["slug"] for item in items))

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

    def test_user_state_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)

            anime_id = server.get_anime_list(db_path)[0]["id"]
            saved = server.update_user_state(
                anime_id,
                {"is_favorite": True, "progress_episode_number": 7, "watched": False},
                db_path,
            )
            self.assertTrue(saved["is_favorite"])
            self.assertEqual(saved["progress_episode_number"], 7)
            self.assertFalse(saved["watched"])

            item = next(item for item in server.get_anime_list(db_path) if item["id"] == anime_id)
            self.assertTrue(item["is_favorite"])
            self.assertEqual(item["progress_episode_number"], 7)
            self.assertFalse(item["watched"])

            server.update_user_state(anime_id, {"watched": True}, db_path)
            detail = server.get_anime_detail(anime_id, db_path)
            self.assertTrue(detail["is_favorite"])
            self.assertEqual(detail["progress_episode_number"], 7)
            self.assertTrue(detail["watched"])

    def test_recommendations_are_ranked_and_exclude_known_titles(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)

            items = server.get_anime_list(db_path)
            favorite = next(item for item in items if item.get("genres") and item.get("source_count") > 0)
            server.update_user_state(favorite["id"], {"is_favorite": True}, db_path)

            payload = server.get_recommendations(db_path, limit=20)
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
        self.assertIn('id="fullscreen-toggle"', html)
        self.assertIn('id="pip-toggle"', html)
        self.assertIn('id="recommendation-meta"', html)
        self.assertIn('href="/static/favicon.svg"', html)
        self.assertIn('href="/favicon.ico"', html)

    def test_right_pane_deep_links_are_supported(self):
        js = Path(server.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn("readLinkState", js)
        self.assertIn("syncUrlFromDetail", js)
        self.assertIn("window.history", js)

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


if __name__ == "__main__":
    unittest.main()
