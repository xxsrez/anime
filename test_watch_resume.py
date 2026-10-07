import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

import server
import test_algorithm_correctness
import test_app
from scripts import db_migrate


class WatchResumeTest(unittest.TestCase):
    def setUp(self):
        self.fixture = test_algorithm_correctness.PlaybackAlgorithmCorrectnessTest()
        self.fixture.setUp()
        self.db = self.fixture.db_path
        self.user = self.fixture.user_id
        self.fixture.seed_title(901, "Resume regression", episode_count=23)
        self.detail = server.get_anime_detail(901, self.db, self.user)

    def tearDown(self):
        self.fixture.tearDown()

    def event(self, episode, event_type="player_engaged", *, day=12, session=None, **extra):
        payload = self.fixture.watch_payload(self.detail, episode, "AniDUB", session or f"tab-{episode}")
        payload.update(event_type=event_type, **extra)
        with patch("server.now_iso", return_value=f"2026-09-{day:02}T16:00:00+00:00"):
            return server.record_watch_event(payload, self.db, self.user)

    def target(self):
        return server.get_continue_watching(self.db, self.user)["item"]

    def test_dormant_tab_does_not_replace_22_with_16(self):
        self.event(15, day=4)
        with patch.object(server, "WATCH_LIKELY_COMPLETED_SECONDS", 60):
            self.event(15, "heartbeat", day=4, engaged_seconds=60)
        self.event(22)
        before = self.fixture.episode_state(901, 15)
        for event_type in ("page_hidden", "player_loaded", "episode_selected", "source_changed", "session_end", "heartbeat"):
            with self.subTest(event_type=event_type):
                self.event(15, event_type, day=13, engaged_seconds=0)
                self.assertEqual(self.target()["episode_number"], "22")
                self.assertEqual(self.fixture.episode_state(901, 15), before)
                self.assertEqual(server.get_anime_detail(901, self.db, self.user)["progress_episode_number"], 22)
        con = server.connect(self.db)
        try:
            self.assertEqual(con.execute("select count(*) from user_watch_events where event_type='page_hidden'").fetchone()[0], 1)
        finally:
            con.close()

    def test_delayed_tail_counts_seconds_without_reordering_history(self):
        self.event(15, day=4)
        self.event(22)
        self.event(15, "page_hidden", day=13, engaged_seconds=7)
        self.assertEqual(self.fixture.episode_state(901, 15)["engaged_seconds"], 7)
        self.assertEqual(self.target()["episode_number"], "22")
        # A deliberate replay still takes priority over higher episode numbers.
        self.event(15, day=14, session="deliberate-replay")
        self.assertEqual(self.target()["episode_number"], "15")

    def test_ended_completes_under_18_minutes_and_survives_pause(self):
        self.event(22)
        for seconds in (300, 300, 300, 137):
            self.event(22, "heartbeat", engaged_seconds=seconds)
        self.event(22, "session_end")  # pause emitted immediately before ended
        self.assertIsNone(self.fixture.episode_state(901, 22)["completed_at"])
        self.event(22, "session_end", playback_ended=True)
        self.assertEqual(self.fixture.episode_state(901, 22)["engaged_seconds"], 1037)
        self.assertEqual(self.target()["episode_number"], "23")
        self.assertEqual(self.target()["reason"], "next_episode")
        self.assertIsNotNone(server.get_anime_detail(901, self.db, self.user)["last_watch"]["completed_at"])

    def test_ended_without_current_session_playback_cannot_complete(self):
        self.event(22)
        self.event(22, "heartbeat", engaged_seconds=60)
        self.event(22, "session_end", session="idle-tab", playback_ended=True)
        self.assertIsNone(self.fixture.episode_state(901, 22)["completed_at"])
        with self.assertRaisesRegex(ValueError, "requires session_end"):
            self.event(22, "player_loaded", playback_ended=True)
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            self.event(22, "session_end", playback_ended="true")

    def test_late_ended_does_not_override_newer_episode(self):
        self.event(15, day=4)
        self.event(15, "heartbeat", day=4, engaged_seconds=5)
        self.event(22)
        self.event(15, "session_end", day=13, playback_ended=True)
        self.assertIsNotNone(self.fixture.episode_state(901, 15)["completed_at"])
        self.assertEqual(self.target()["episode_number"], "22")

    def test_ended_before_final_seconds_is_reconciled(self):
        self.event(22)
        self.event(22, "session_end", playback_ended=True)
        self.assertIsNone(self.fixture.episode_state(901, 22)["completed_at"])
        self.event(22, "session_end", engaged_seconds=10)
        self.assertEqual(self.target()["episode_number"], "23")

    def test_near_end_position_completes_without_ended_or_eighteen_minutes(self):
        self.event(22)
        self.event(22, "heartbeat", engaged_seconds=60)
        result = self.event(22, "session_end", playback_position_seconds=1320, playback_duration_seconds=1380)
        self.assertIsNotNone(result["episode_state"]["completed_at"])
        self.assertEqual(self.target()["episode_number"], "23")

    def test_position_alone_or_old_session_does_not_complete(self):
        self.event(22)
        self.event(22, "heartbeat", engaged_seconds=60)
        self.event(22, "session_end", playback_position_seconds=1200, playback_duration_seconds=1380)
        self.assertIsNone(self.fixture.episode_state(901, 22)["completed_at"])
        self.event(22, "session_end", session="new-seek", playback_position_seconds=1370, playback_duration_seconds=1380)
        self.assertIsNone(self.fixture.episode_state(901, 22)["completed_at"])
        with self.assertRaises(ValueError):
            self.event(22, "heartbeat", playback_position_seconds=True, playback_duration_seconds=1380)

    def set_release_status(self, status, total="23"):
        con = server.connect(self.db)
        con.execute("update anime set status = ?, episodes_text = ? where id = 901", (status, total))
        con.commit()
        con.close()
        server.invalidate_catalog_cache(self.db)

    def test_final_episode_ends_finished_title_and_preserves_favorite(self):
        self.set_release_status("Вышел")
        server.update_user_state(901, {"is_favorite": True}, self.db, self.user)
        self.event(23)
        self.event(23, "heartbeat", engaged_seconds=10)
        result = self.event(23, "session_end", playback_ended=True)
        self.assertEqual(result["state"]["watch_status"], "completed")
        self.assertTrue(result["state"]["watched"])
        self.assertTrue(result["state"]["is_favorite"])
        self.assertIsNone(self.target())

    def test_completed_ongoing_title_is_reconciled_when_release_finishes(self):
        self.set_release_status("Онгоинг")
        self.event(23)
        self.event(23, "session_end", playback_ended=True)
        result = self.event(23, "session_end", engaged_seconds=10)
        self.assertEqual(result["state"]["watch_status"], "watching")
        self.set_release_status("Вышел")
        items = server.get_anime_list(self.db, user_id=self.user)
        self.assertEqual(next(item for item in items if item["id"] == 901)["watch_status"], "completed")
        con = server.connect(self.db)
        self.assertEqual(con.execute("select watch_status from user_title_state where user_id=? and anime_id=901", (self.user,)).fetchone()[0], "completed")
        con.close()

    def test_final_episode_selection_or_incomplete_import_is_not_completion(self):
        self.set_release_status("Вышел")
        server.update_user_state(901, {"progress_episode_number": 23}, self.db, self.user)
        self.assertEqual(server.get_anime_detail(901, self.db, self.user)["watch_status"], "watching")
        self.set_release_status("Вышел", "24")
        self.event(23)
        self.event(23, "heartbeat", engaged_seconds=10)
        self.event(23, "session_end", playback_ended=True)
        self.assertEqual(server.get_anime_detail(901, self.db, self.user)["watch_status"], "watching")

    def test_unknown_release_status_does_not_auto_complete(self):
        self.event(23)
        self.event(23, "heartbeat", engaged_seconds=10)
        self.event(23, "session_end", playback_ended=True)
        self.assertEqual(server.get_anime_detail(901, self.db, self.user)["watch_status"], "watching")

    def test_completion_checks_extra_fractional_episode_from_other_source(self):
        self.fixture.seed_title(902, "Resume regression", source="yummyanime", episode_count=24)
        con = server.connect(self.db)
        con.execute("update episodes set number='23.5' where id=90224")
        con.commit()
        con.close()
        self.set_release_status("Вышел")
        self.detail = server.get_anime_detail(901, self.db, self.user)
        self.event(23)
        self.event(23, "heartbeat", engaged_seconds=10)
        result = self.event(23, "session_end", playback_ended=True)
        self.assertEqual(result["state"]["watch_status"], "watching")

    def test_background_duplicate_does_not_hide_completed_finale(self):
        self.fixture.seed_title(902, "Resume regression", source="yummyanime", episode_count=23)
        self.set_release_status("Онгоинг")
        self.event(23)
        self.event(23, "heartbeat", engaged_seconds=10)
        self.event(23, "session_end", playback_ended=True)
        con = server.connect(self.db)
        con.execute("""insert into user_episode_state
            (user_id,anime_id,episode_id,progress_episode_number,first_seen_at,last_seen_at,updated_at,last_event_type)
            values (?,901,90223,23,'2026-09-13','2026-09-13','2026-09-13','player_loaded')""", (self.user,))
        con.commit()
        con.close()
        self.set_release_status("Вышел")
        self.assertEqual(server.get_anime_detail(901, self.db, self.user)["watch_status"], "completed")

    def test_passive_events_preserve_manual_progress_and_source(self):
        self.event(15, day=4)
        with patch("server.now_iso", return_value="2026-09-12T16:00:00+00:00"):
            server.update_user_state(901, {"progress_episode_number": 22}, self.db, self.user)
        before = self.fixture.episode_state(901, 22)
        self.event(22, "source_changed", day=13, session="idle-tab")
        self.assertEqual(self.fixture.episode_state(901, 22), before)
        server.update_user_state(901, {"progress_episode_number": None}, self.db, self.user)
        self.event(22, "page_hidden", day=14)
        self.assertIsNone(self.fixture.episode_state(901, 22)["started_at"])

    def test_repair_restores_legacy_recency_without_inventing_completion(self):
        self.event(15, day=4)
        self.event(22)
        self.event(15, "page_hidden", day=13)
        con = server.connect(self.db)
        try:
            # Simulate the pre-fix aggregate. Keep the original raw history.
            con.execute("update user_episode_state set last_seen_at='2026-09-13T16:00:00+00:00', last_event_type='page_hidden' where progress_episode_number=15")
            con.commit()
            sql = (Path(__file__).parent / "migrations/2026-09-18_watch-recency/01_restore_watch_recency.sql").read_text()
            con.executescript(sql)
            con.executescript(sql)  # safe to repeat without moving dates forward
        finally:
            con.close()
        self.assertEqual(self.target()["episode_number"], "22")
        self.assertIsNone(self.fixture.episode_state(901, 22)["completed_at"])

    def test_repair_runner_preserves_manual_progress_and_cleared_history(self):
        self.event(15, day=4)
        self.event(22)
        server.update_user_state(901, {"progress_episode_number": 22}, self.db, self.user)
        manual = self.fixture.episode_state(901, 22)
        migration_folder = Path(__file__).parent / "migrations/2026-09-18_watch-recency"
        migration_root = Path(self.fixture.tmpdir.name) / "migrations"
        shutil.copytree(migration_folder, migration_root / migration_folder.name)
        result = db_migrate.apply_pending(self.db, [migration_root], no_backup=True)
        self.assertEqual(len(result["applied"]), 1)
        self.assertEqual(self.fixture.episode_state(901, 22), manual)
        server.update_user_state(901, {"progress_episode_number": None}, self.db, self.user)
        result = db_migrate.apply_pending(self.db, [migration_root], no_backup=True)
        self.assertEqual(result["applied"], [])
        self.assertIsNone(self.fixture.episode_state(901, 22)["started_at"])

    def test_real_frontend_ended_payload_reaches_backend_continue(self):
        output = subprocess.check_output(["node", "static/watch_tracking.test.js", "--payloads"], cwd=Path(__file__).parent, text=True)
        client = test_app.LocalAppTest()
        token = client.create_session(self.db, self.user)
        for event in json.loads(output):
            payload = self.fixture.watch_payload(self.detail, 22, "AniDUB", "browser-session")
            payload.update(event)
            status, _, body = client.request_test_server(
                self.db, "POST", "/api/watch-events",
                headers={"Cookie": f"{server.SESSION_COOKIE_NAME}={token}", "Content-Type": "application/json"},
                body=json.dumps(payload).encode(),
            )
            self.assertEqual(status, 200, body)
        self.assertEqual(self.target()["episode_number"], "23")


if __name__ == "__main__":
    unittest.main()
