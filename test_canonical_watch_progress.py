import unittest
from unittest.mock import patch

import content_updates
import server
import test_algorithm_correctness


class CanonicalWatchProgressTest(unittest.TestCase):
    def setUp(self):
        self.fixture = test_algorithm_correctness.PlaybackAlgorithmCorrectnessTest()
        self.fixture.setUp()
        self.db = self.fixture.db_path
        self.user = self.fixture.user_id
        self.fixture.seed_title(901, "Duplicate episode regression", episode_count=1)
        self.fixture.seed_title(902, "Duplicate episode regression", source="yummyanime", episode_count=2)
        self.before = server.get_anime_detail(901, self.db, self.user)
        self.old_payload = self.fixture.watch_payload(self.before, 2, "AniDUB", "original-session")
        self.assertEqual(self.old_payload["episode_id"], 90202)

    def tearDown(self):
        self.fixture.tearDown()

    def event(self, payload, timestamp, **changes):
        with patch("server.now_iso", return_value=timestamp):
            return server.record_watch_event({**payload, **changes}, self.db, self.user)

    def add_primary_episode(self):
        con = server.connect(self.db)
        try:
            con.execute("""insert into episodes (id, anime_id, number, has_video, scraped_at)
                           values (90102, 901, '2', 1, '2026-11-13T14:40:26+00:00')""")
            con.execute("""insert into video_sources
                           (anime_id, episode_id, provider_id, provider_title, translation_id,
                            translation_title, embed_host, embed_url, embed_url_redacted, scraped_at)
                           values (901, 90102, 'kodik-primary-2', 'Kodik', '1', 'AniDUB',
                                   'kodikplayer.com', 'https://kodikplayer.com/901/2',
                                   'https://kodikplayer.com/901/2', '2026-11-13T14:40:26+00:00')""")
            con.commit()
        finally:
            con.close()
        server.invalidate_catalog_cache(self.db)
        detail = server.get_anime_detail(901, self.db, self.user)
        payload = self.fixture.watch_payload(detail, 2, "AniDUB", "later-session")
        self.assertEqual(payload["episode_id"], 90102)
        return payload

    def complete_original_watch(self):
        self.event(self.old_payload, "2026-11-13T14:39:16+00:00")
        new_payload = self.add_primary_episode()
        self.event(self.old_payload, "2026-11-13T15:00:00+00:00", event_type="heartbeat",
                   engaged_seconds=60, playback_position_seconds=1200, playback_duration_seconds=1422)
        result = self.event(self.old_payload, "2026-11-13T15:01:23+00:00", event_type="session_end",
                            playback_position_seconds=1355, playback_duration_seconds=1422)
        self.assertIsNotNone(result["episode_state"]["completed_at"])
        return new_payload

    def insert_legacy_duplicate(self):
        new_payload = self.complete_original_watch()
        con = server.connect(self.db)
        try:
            con.execute("""insert into user_episode_state
                           (user_id, anime_id, episode_id, episode_number, progress_episode_number,
                            video_source_id, source, source_anime_id, translation_id, translation_title,
                            provider_id, provider_title, embed_host, first_seen_at, last_seen_at,
                            started_at, engaged_seconds, heartbeat_count, last_event_type,
                            last_confidence, updated_at)
                           values (?, 901, 90102, '2', 2, ?, ?, ?, ?, ?, ?, ?, ?,
                                   '2026-11-13T15:01:24+00:00', '2026-11-13T15:02:13+00:00',
                                   '2026-11-13T15:01:24+00:00', 49, 1, 'heartbeat', 0.8,
                                   '2026-11-13T15:02:13+00:00')""",
                        (self.user, new_payload["video_source_id"], new_payload["source"],
                         new_payload["source_anime_id"], new_payload["translation_id"],
                         new_payload["translation_title"], new_payload["provider_id"],
                         new_payload["provider_title"], new_payload["embed_host"]))
            con.commit()
        finally:
            con.close()
        return new_payload

    def test_new_canonical_episode_does_not_hide_completed_watch(self):
        new_payload = self.complete_original_watch()
        self.event(new_payload, "2026-11-13T15:01:24+00:00")
        result = self.event(new_payload, "2026-11-13T15:02:13+00:00",
                            event_type="heartbeat", engaged_seconds=49)
        detail = server.get_anime_detail(901, self.db, self.user)
        catalog = next(item for item in server.get_anime_list(self.db, user_id=self.user) if item["id"] == 901)
        target = server.get_continue_watching(self.db, self.user)["item"]
        with self.subTest(surface="watch event response"):
            self.assertIsNotNone(result["episode_state"]["completed_at"])
        with self.subTest(surface="detail"):
            self.assertTrue(detail["episodes"][-1]["completed"])
            self.assertIsNotNone(detail["last_watch"]["completed_at"])
        with self.subTest(surface="library and updates"):
            self.assertIsNotNone(catalog["last_watch"]["completed_at"])
            self.assertFalse(server.content_update_item_has_unseen_episode(catalog, ["2"]))
        with self.subTest(surface="continue watching"):
            self.assertEqual(target["reason"], "latest_completed")

    def test_existing_split_history_remains_completed(self):
        new_payload = self.insert_legacy_duplicate()
        detail = server.get_anime_detail(901, self.db, self.user)
        catalog = next(item for item in server.get_anime_list(self.db, user_id=self.user) if item["id"] == 901)
        target = server.get_continue_watching(self.db, self.user)["item"]
        with self.subTest(surface="detail"):
            self.assertTrue(detail["episodes"][-1]["completed"])
            self.assertIsNotNone(detail["last_watch"]["completed_at"])
            self.assertEqual(detail["last_watch"]["video_source_id"], new_payload["video_source_id"])
        with self.subTest(surface="library"):
            self.assertIsNotNone(catalog["last_watch"]["completed_at"])
            self.assertFalse(server.content_update_item_has_unseen_episode(catalog, ["2"]))
        with self.subTest(surface="continue watching"):
            self.assertEqual(target["reason"], "latest_completed")
        with self.subTest(surface="watch event response"):
            result = self.event(new_payload, "2026-11-13T15:03:00+00:00")
            self.assertIsNotNone(result["episode_state"]["completed_at"])

    def test_clear_progress_then_rewatch_does_not_reuse_duplicate_completion(self):
        new_payload = self.insert_legacy_duplicate()
        with patch("server.now_iso", return_value="2026-11-13T15:05:00+00:00"):
            server.update_user_state(901, {"watch_status": "none"}, self.db, self.user)
        result = self.event({**new_payload, "client_session_id": "rewatch"}, "2026-11-13T15:06:00+00:00")
        detail = server.get_anime_detail(901, self.db, self.user)
        catalog = next(item for item in server.get_anime_list(self.db, user_id=self.user) if item["id"] == 901)
        with self.subTest(surface="watch event response"):
            self.assertIsNone(result["episode_state"]["completed_at"])
        with self.subTest(surface="detail"):
            self.assertFalse(detail["episodes"][-1]["completed"])
            self.assertIsNone(detail["last_watch"]["completed_at"])
        with self.subTest(surface="library"):
            self.assertIsNone(catalog["last_watch"]["completed_at"])
            self.assertTrue(server.content_update_item_has_unseen_episode(catalog, ["2"]))
        with self.subTest(surface="continue watching"):
            self.assertEqual(server.get_continue_watching(self.db, self.user)["item"]["reason"], "resume")

    def test_update_feed_does_not_prioritize_an_already_completed_episode(self):
        self.insert_legacy_duplicate()
        con = server.connect(self.db)
        try:
            content_updates.insert_event(con, None, "new_episode", 901, source="animego",
                                         source_id="901", episode_number="2", title="Duplicate episode regression",
                                         occurred_at=server.now_iso(), dedupe_key="episode-2-scan")
            con.commit()
        finally:
            con.close()
        result = server.get_content_updates(self.db, user_id=self.user)
        item = next(item for item in result["items"] if item["id"] == 901)
        self.assertFalse(item["has_unseen_episode"])
        self.assertFalse(item["is_priority"])

    def test_completed_final_episode_is_reconciled_across_duplicate_rows(self):
        self.insert_legacy_duplicate()
        con = server.connect(self.db)
        try:
            con.execute("update anime set status = 'Вышел', episodes_text = '2' where id in (901, 902)")
            con.commit()
        finally:
            con.close()
        server.invalidate_catalog_cache(self.db)
        self.assertEqual(server.get_anime_detail(901, self.db, self.user)["watch_status"], "completed")

    def test_next_episode_is_selected_after_existing_split_history(self):
        self.insert_legacy_duplicate()
        con = server.connect(self.db)
        try:
            con.execute("""insert into episodes (id, anime_id, number, has_video, scraped_at)
                           values (90103, 901, '3', 1, ?)""", (server.now_iso(),))
            con.execute("""insert into video_sources
                           (anime_id, episode_id, provider_id, provider_title, translation_id,
                            translation_title, embed_host, embed_url, embed_url_redacted, scraped_at)
                           values (901, 90103, 'kodik-primary-3', 'Kodik', '1', 'AniDUB',
                                   'kodikplayer.com', 'https://kodikplayer.com/901/3',
                                   'https://kodikplayer.com/901/3', ?)""", (server.now_iso(),))
            con.commit()
        finally:
            con.close()
        server.invalidate_catalog_cache(self.db)
        target = server.get_continue_watching(self.db, self.user)["item"]
        self.assertEqual((target["episode_number"], target["reason"]), ("3", "next_episode"))

    def test_later_completion_keeps_the_most_recent_source(self):
        self.event(self.old_payload, "2026-11-13T14:39:16+00:00")
        new_payload = self.add_primary_episode()
        self.event(self.old_payload, "2026-11-13T15:00:00+00:00", event_type="heartbeat",
                   engaged_seconds=60, playback_position_seconds=1200, playback_duration_seconds=1422)
        self.event(new_payload, "2026-11-13T15:01:24+00:00")
        self.event(self.old_payload, "2026-11-13T15:02:13+00:00", event_type="session_end",
                   playback_position_seconds=1355, playback_duration_seconds=1422)
        detail = server.get_anime_detail(901, self.db, self.user)
        self.assertIsNotNone(detail["last_watch"]["completed_at"])
        self.assertEqual(detail["last_watch"]["video_source_id"], new_payload["video_source_id"])
        self.assertEqual(detail["last_watch"]["last_seen_at"], "2026-11-13T15:01:24+00:00")

    def test_completion_is_isolated_per_user(self):
        new_payload = self.insert_legacy_duplicate()
        con = server.connect(self.db)
        try:
            other = server.upsert_google_user(con, {
                "sub": "other-playback-user", "email": "other-playback@example.test",
                "email_verified": True, "name": "Other Viewer", "picture": None,
            })["id"]
            con.commit()
        finally:
            con.close()
        with patch("server.now_iso", return_value="2026-11-13T15:03:00+00:00"):
            result = server.record_watch_event({**new_payload, "client_session_id": "other"}, self.db, other)
        self.assertIsNone(result["episode_state"]["completed_at"])
        detail = server.get_anime_detail(901, self.db, other)
        self.assertFalse(detail["episodes"][-1]["completed"])
        self.assertIsNone(detail["last_watch"]["completed_at"])

    def test_completed_legacy_row_without_episode_number_uses_progress_number(self):
        self.insert_legacy_duplicate()
        con = server.connect(self.db)
        try:
            con.execute("update user_episode_state set episode_number = null where user_id = ? and episode_id = 90202",
                        (self.user,))
            con.commit()
        finally:
            con.close()
        detail = server.get_anime_detail(901, self.db, self.user)
        self.assertTrue(detail["episodes"][-1]["completed"])
        self.assertIsNotNone(detail["last_watch"]["completed_at"])

    def test_completed_legacy_row_can_fall_back_to_episode_zero(self):
        self.insert_legacy_duplicate()
        con = server.connect(self.db)
        try:
            con.execute("update episodes set number = '0' where id in (90102, 90202)")
            con.execute("update user_episode_state set episode_number = '0', progress_episode_number = 0 where user_id = ?",
                        (self.user,))
            con.execute("update user_episode_state set episode_number = null where user_id = ? and episode_id = 90202",
                        (self.user,))
            con.execute("update user_title_state set progress_episode_number = 0 where user_id = ?", (self.user,))
            con.commit()
        finally:
            con.close()
        server.invalidate_catalog_cache(self.db)
        detail = server.get_anime_detail(901, self.db, self.user)
        episode = next(item for item in detail["episodes"] if item["number"] == "0")
        self.assertTrue(episode["completed"])
        self.assertIsNotNone(detail["last_watch"]["completed_at"])

    def test_completion_is_not_shared_with_another_episode(self):
        first = self.fixture.watch_payload(self.before, 1, "AniDUB", "first-episode")
        self.event(first, "2026-11-13T14:30:00+00:00")
        self.event(first, "2026-11-13T14:31:00+00:00", event_type="heartbeat", engaged_seconds=60)
        self.event(first, "2026-11-13T14:32:00+00:00", event_type="session_end", playback_ended=True)
        result = self.event(self.old_payload, "2026-11-13T14:39:16+00:00")
        detail = server.get_anime_detail(901, self.db, self.user)
        self.assertIsNone(result["episode_state"]["completed_at"])
        self.assertFalse(detail["episodes"][-1]["completed"])
        self.assertIsNone(detail["last_watch"]["completed_at"])

    def test_finished_title_can_be_rewatched_after_split_history_is_cleared(self):
        new_payload = self.insert_legacy_duplicate()
        con = server.connect(self.db)
        try:
            con.execute("update anime set status = 'Вышел', episodes_text = '2' where id in (901, 902)")
            con.commit()
        finally:
            con.close()
        server.invalidate_catalog_cache(self.db)
        self.assertEqual(server.get_anime_detail(901, self.db, self.user)["watch_status"], "completed")
        with patch("server.now_iso", return_value="2026-11-13T15:05:00+00:00"):
            server.update_user_state(901, {"watch_status": "none"}, self.db, self.user)
        self.event({**new_payload, "client_session_id": "rewatch"}, "2026-11-13T15:06:00+00:00")
        detail = server.get_anime_detail(901, self.db, self.user)
        self.assertEqual(detail["watch_status"], "watching")
        self.assertFalse(detail["episodes"][-1]["completed"])
        self.assertIsNone(detail["last_watch"]["completed_at"])


if __name__ == "__main__":
    unittest.main()
