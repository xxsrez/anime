from contextlib import closing
from pathlib import Path
import tempfile
import unittest

import server
from scripts.import_mal_ratings import match_catalog


class MalRatingsTest(unittest.TestCase):
    def test_matching_uses_explicit_id_or_unique_season_and_episode_count(self):
        rows = [
            {"id": 1, "title": "Title", "subtitle": "", "year": "2025", "status": "Вышел", "episodes_text": "12"},
            {"id": 2, "title": "Title", "subtitle": "", "year": "2025", "status": "Онгоинг", "episodes_text": "12"},
            {"id": 3, "title": "Title", "subtitle": "", "year": "2024"},
            {"id": 4, "title": "Other", "subtitle": "", "year": "2025"},
        ]
        entries = [
            {"title": "Title", "episodes": count, "animeSeason": {"year": year},
             "sources": [f"https://myanimelist.net/anime/{mal_id}"]}
            for mal_id, count, year in [(100, 12, 2025), (101, 13, 2025), (102, 12, 2024)]
        ]
        matches, stats = match_catalog(rows, {4: {91}, 3: {102, 103}}, entries)
        self.assertEqual(matches, {1: 100, 4: 91})
        self.assertEqual(stats["ambiguous"], 2)

    def test_mal_scores_are_consistent_in_list_detail_and_recommendations(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.sqlite"
            with closing(server.connect(path)) as con, con:
                con.executemany(
                    "insert into anime(id,slug,title,url,scraped_at) values (?,?,?,?,?)",
                    [(1, "rated", "Rated", "https://animego.me/anime/rated-1", "2026-11-19"),
                     (2, "unrated", "Unrated", "https://animego.me/anime/unrated-2", "2026-11-19")],
                )
            with closing(server.connect(path)) as con, con:
                con.executemany(
                    "insert into anime_fields(anime_id,label,value) values (?,?,?)",
                    [(2, "MyAnimeList ID", "112"), (2, "MyAnimeList", "7.4"), (2, "Shikimori", "9.2")],
                )
            before = {item["id"]: item for item in server.get_anime_list(path)}
            self.assertEqual(before[2]["mal_score"], 7.4)
            with closing(server.connect(path)) as con, con:
                con.executemany(
                    "insert into anime_mal_ratings values (?,?,?,?,?)",
                    [(1, 111, 8.41, 1100, "2026-11-19"), (2, 112, None, None, "2026-11-19")],
                )
            after = {item["id"]: item for item in server.get_anime_list(path)}
            self.assertEqual(server.catalog_api_item(after[1])["mal_score"], 8.41)
            self.assertIsNone(server.catalog_api_item(after[2])["mal_score"])
            self.assertEqual(server.get_anime_detail(1, path)["mal_id"], 111)
            self.assertIsNone(server.get_anime_detail(2, path)["mal_score"])
            ranked = server.get_recommendations(path)["items"]
            self.assertEqual([item["mal_score"] for item in ranked], [8.41, None])
            self.assertGreater(ranked[0]["recommendation_score"], ranked[1]["recommendation_score"])

    def test_conflicting_mal_ids_on_merged_card_do_not_supply_a_rating(self):
        one = {"id": 1, "title": "Variant", "mal_id": 111, "mal_score": 8.4}
        two = {"id": 2, "title": "Variant", "mal_id": 112, "mal_score": 9.4}
        group = server.merge_canonical_items([one, two])
        self.assertIsNone(group["mal_id"])
        self.assertIsNone(group["mal_score"])
