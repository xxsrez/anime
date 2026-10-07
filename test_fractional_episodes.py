import unittest
from unittest.mock import patch

import scrape_yummyanime
import scrape_animego
import server
import sync_videos
import test_algorithm_correctness
import user_state_model


class FractionalEpisodesTest(unittest.TestCase):
    def setUp(self):
        self.fixture = test_algorithm_correctness.PlaybackAlgorithmCorrectnessTest()
        self.fixture.setUp()
        self.db = self.fixture.db_path
        self.user = self.fixture.user_id
        anime = {
            "anime_id": 15, "anime_url": "fractional-test", "title": "Fractional test",
            "episodes": {"count": 1168},
            "videos": [
                {"number": number, "video_id": index,
                 "iframe_url": f"https://kodikplayer.com/seria/{index}/token",
                 "data": {"player": "Плеер Kodik", "dubbing": "Test"}}
                for index, number in enumerate(["1169", "1168.50", "1168"], start=1)
            ],
        }
        with patch.object(scrape_yummyanime, "fetch_modern_anime", return_value=(anime, "https://api.test/15")):
            self.parsed = scrape_yummyanime.parse_modern_detail("https://ru.yummyani.me/catalog/item/fractional-test")
        self.anime_id = self.parsed[0]["id"]
        self.import_title()
        self.detail = server.get_anime_detail(self.anime_id, self.db, self.user)

    def tearDown(self):
        self.fixture.tearDown()

    def import_title(self):
        args = sync_videos.parse_args(["--mode", "manual", "--yummy-ref", "fractional-test"])
        con = sync_videos.connect(self.db)
        try:
            with patch("sync_videos.parse_yummy_detail_for_sync", return_value=self.parsed):
                sync_videos.sync_yummy_title(con, "fractional-test", args, sync_videos.defaultdict(int), "manual")
            con.commit()
        finally:
            con.close()
        server.invalidate_catalog_cache(self.db)

    def payload(self, number):
        episode = next(e for e in self.detail["episodes"] if e["number"] == str(number))
        source = self.detail["sources_by_episode"][episode["id"]][0]
        return {
            "anime_id": self.anime_id, "episode_id": episode["id"], "episode_number": str(number),
            "progress_episode_number": number, "video_source_id": source["id"],
            "client_session_id": f"fractional-{number}", "event_type": "player_engaged",
            "page_visible": True, "player_focused": True,
        }

    def test_import_keeps_distinct_ids_and_is_idempotent(self):
        self.assertEqual([e["number"] for e in self.detail["episodes"]], ["1168", "1168.5", "1169"])
        ids = [e["id"] for e in self.detail["episodes"]]
        self.assertEqual(ids[0], self.anime_id * 1000 + 1168)
        self.assertEqual(ids[2], self.anime_id * 1000 + 1169)
        self.assertGreaterEqual(ids[1], 2**52)
        self.import_title()
        repeated = server.get_anime_detail(self.anime_id, self.db, self.user)
        self.assertEqual([e["id"] for e in repeated["episodes"]], ids)
        self.assertEqual([e["source_count"] for e in repeated["episodes"]], [1, 1, 1])

    def test_fractional_progress_resume_completion_and_next_episode(self):
        with patch("server.now_iso", return_value="2026-10-07T12:00:00+00:00"):
            server.record_watch_event(self.payload(1168), self.db, self.user)
        special = self.payload(1168.5)
        server.record_watch_event(special, self.db, self.user)
        detail = server.get_anime_detail(self.anime_id, self.db, self.user)
        self.assertEqual(detail["progress_episode_number"], 1168.5)
        self.assertEqual(detail["last_watch"]["episode_id"], special["episode_id"])
        target = server.get_continue_watching(self.db, self.user)["item"]
        self.assertEqual(target["episode_number"], "1168.5")
        server.record_watch_event({**special, "event_type": "heartbeat", "engaged_seconds": 10}, self.db, self.user)
        server.record_watch_event({**special, "event_type": "session_end", "playback_ended": True}, self.db, self.user)
        self.assertEqual(server.get_continue_watching(self.db, self.user)["item"]["episode_number"], "1169")
        con = server.connect(self.db)
        try:
            states = list(con.execute("select progress_episode_number,engaged_seconds,completed_at from user_episode_state order by progress_episode_number"))
            self.assertEqual([r[0] for r in states], [1168, 1168.5])
            self.assertEqual(states[0][1], 0)
            self.assertIsNone(states[0][2])
            self.assertEqual(states[1][1], 10)
            self.assertIsNotNone(states[1][2])
        finally:
            con.close()

    def test_manual_progress_and_number_only_event_select_fractional_episode(self):
        special = self.payload(1168.5)
        state = server.update_user_state(self.anime_id, {"progress_episode_number": 1168.5}, self.db, self.user)
        self.assertEqual(state["last_watch"]["episode_id"], special["episode_id"])
        del special["episode_id"]
        server.record_watch_event(special, self.db, self.user)
        self.assertEqual(server.get_continue_watching(self.db, self.user)["item"]["episode_number"], "1168.5")

    def test_regular_provider_or_truncated_progress_cannot_match_fraction(self):
        special = self.payload(1168.5)
        regular = self.payload(1168)
        for field in ("video_source_id", "episode_number", "progress_episode_number"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                server.record_watch_event({**special, field: regular[field]}, self.db, self.user)
        with self.assertRaises(ValueError):
            server.update_user_state(self.anime_id, {
                "progress_episode_number": 1168.5, "video_source_id": regular["video_source_id"],
            }, self.db, self.user)

    def test_nonfinite_and_non_numeric_progress_are_rejected(self):
        for value in (float("nan"), float("inf"), -0.5, True, "1168.5"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                user_state_model.validate_patch({"progress_episode_number": value})

    def test_collision_is_rejected_on_direct_upsert(self):
        con = server.connect(self.db)
        episode = dict(self.detail["episodes"][1])
        episode["number"] = "1168.75"
        try:
            with self.assertRaisesRegex(ValueError, "Episode ID collision"):
                scrape_animego.upsert_episode(con, self.anime_id, episode, True, None, "now")
            self.assertEqual(con.execute("select number from episodes where id=?", (episode["id"],)).fetchone()[0], "1168.5")
        finally:
            con.close()

    def test_collision_in_new_parser_batch_is_rejected(self):
        anime = {
            "anime_id": 16, "anime_url": "collision", "title": "Collision",
            "episodes": {"count": 1},
            "videos": [{"number": "1.5"}, {"number": "1.75"}],
        }
        with patch.object(scrape_yummyanime, "fetch_modern_anime", return_value=(anime, "https://api.test/16")), \
             patch.object(scrape_yummyanime, "internal_episode_id", return_value=2**52), \
             self.assertRaisesRegex(ValueError, "ID collision within anime"):
            scrape_yummyanime.parse_modern_detail("https://ru.yummyani.me/catalog/item/collision")


if __name__ == "__main__":
    unittest.main()
