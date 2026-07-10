import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
from scripts import db_migrate
import test_app


ROOT = Path(__file__).resolve().parent
LIBRARY_MIGRATION = ROOT / "migrations" / server.USER_LIBRARY_MIGRATION_PATH


class UserStateBackendTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "anime.sqlite"
        test_app.LocalAppTest.seed_watchable_title(self, self.db_path, anime_id=10, episode_count=2)
        self.user_id = test_app.LocalAppTest.create_google_user(
            self,
            self.db_path,
            "user-state-backend",
            "state@example.com",
        )

    def tearDown(self):
        server.invalidate_catalog_cache(self.db_path)
        server.reset_database_initialization(self.db_path)
        self.tmpdir.cleanup()

    def detail(self):
        return server.get_anime_detail(10, self.db_path, self.user_id)

    def watch_payload(self, *, event_type="player_engaged", episode_index=0, engaged_seconds=0):
        detail = self.detail()
        payload = test_app.LocalAppTest.watch_payload(
            self,
            detail,
            episode_index=episode_index,
            event_type=event_type,
            session_id=f"state-{event_type}",
        )
        payload["engaged_seconds"] = engaged_seconds
        return payload

    def copy_library_migration(self):
        root = Path(self.tmpdir.name) / "migrations"
        target = root / LIBRARY_MIGRATION.parent.name / LIBRARY_MIGRATION.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(LIBRARY_MIGRATION, target)
        return root

    def test_schema_and_catalog_detail_patch_expose_complete_state(self):
        saved = server.update_user_state(
            10,
            {"is_favorite": True, "watch_status": "planned", "not_interested": True},
            self.db_path,
            self.user_id,
        )
        self.assertEqual(saved["watch_status"], "planned")
        self.assertTrue(saved["not_interested"])
        self.assertIsNotNone(saved["favorite_updated_at"])
        self.assertIsNotNone(saved["watch_status_updated_at"])
        self.assertIsNotNone(saved["not_interested_updated_at"])

        detail = self.detail()
        catalog = next(
            item for item in server.get_anime_list(self.db_path, user_id=self.user_id)
            if item["id"] == 10
        )
        public = server.catalog_api_item(catalog)
        required = {
            "watch_status",
            "not_interested",
            "favorite_updated_at",
            "watch_status_updated_at",
            "not_interested_updated_at",
        }
        self.assertTrue(required <= detail.keys())
        self.assertTrue(required <= public.keys())
        self.assertEqual(public["watch_status"], "planned")
        self.assertTrue(public["not_interested"])

        con = server.connect(self.db_path)
        try:
            columns = {row[1] for row in con.execute("pragma table_info(user_title_state)")}
            self.assertTrue(required <= columns)
        finally:
            con.close()

    def test_latest_field_timestamp_wins_when_canonical_variants_disagree(self):
        state = server.aggregate_state_rows(
            [
                {
                    "anime_id": 1,
                    "is_favorite": 1,
                    "watched": 1,
                    "progress_episode_number": 12,
                    "watch_status": "completed",
                    "not_interested": 0,
                    "updated_at": "2026-07-09T10:00:00+00:00",
                    "favorite_updated_at": "2026-07-09T10:00:00+00:00",
                    "watch_status_updated_at": "2026-07-09T10:00:00+00:00",
                    "not_interested_updated_at": None,
                },
                {
                    "anime_id": 2,
                    "is_favorite": 0,
                    "watched": 0,
                    "progress_episode_number": None,
                    "watch_status": "dropped",
                    "not_interested": 1,
                    "updated_at": "2026-07-09T11:00:00+00:00",
                    "favorite_updated_at": "2026-07-09T11:00:00+00:00",
                    "watch_status_updated_at": "2026-07-09T11:00:00+00:00",
                    "not_interested_updated_at": "2026-07-09T11:00:00+00:00",
                },
            ]
        )
        self.assertEqual(state["watch_status"], "dropped")
        self.assertFalse(state["watched"])
        self.assertIsNone(state["progress_episode_number"])
        self.assertFalse(state["is_favorite"])
        self.assertTrue(state["not_interested"])

    def test_status_only_dropped_and_planned_clear_episode_state_and_continue(self):
        server.record_watch_event(self.watch_payload(), self.db_path, self.user_id)
        self.assertEqual(self.detail()["watch_status"], "watching")
        self.assertIsNotNone(server.get_continue_watching(self.db_path, self.user_id)["item"])

        for status in ("dropped", "planned"):
            if status == "planned":
                server.update_user_state(
                    10,
                    {"progress_episode_number": 1, "watch_status": "watching"},
                    self.db_path,
                    self.user_id,
                )
            saved = server.update_user_state(
                10,
                {"watch_status": status},
                self.db_path,
                self.user_id,
            )
            self.assertEqual(saved["watch_status"], status)
            self.assertIsNone(saved["progress_episode_number"])
            self.assertIsNone(server.get_continue_watching(self.db_path, self.user_id)["item"])
            con = server.connect(self.db_path)
            try:
                started = con.execute(
                    "select count(*) from user_episode_state where user_id = ? and started_at is not null",
                    (self.user_id,),
                ).fetchone()[0]
                self.assertEqual(started, 0)
            finally:
                con.close()

    def test_continue_prefilters_completed_history_before_loading_details(self):
        server.record_watch_event(self.watch_payload(), self.db_path, self.user_id)
        server.update_user_state(
            10,
            {"watch_status": "completed"},
            self.db_path,
            self.user_id,
        )

        with patch("server.get_anime_detail") as get_detail:
            payload = server.get_continue_watching(self.db_path, self.user_id)

        self.assertIsNone(payload["item"])
        get_detail.assert_not_called()

    def test_inflight_heartbeat_cannot_undo_explicit_pause(self):
        server.record_watch_event(self.watch_payload(), self.db_path, self.user_id)
        server.update_user_state(
            10,
            {"watch_status": "paused"},
            self.db_path,
            self.user_id,
        )

        result = server.record_watch_event(
            self.watch_payload(event_type="heartbeat", engaged_seconds=10),
            self.db_path,
            self.user_id,
        )

        self.assertEqual(result["state"]["watch_status"], "paused")
        self.assertEqual(self.detail()["watch_status"], "paused")
        # Paused titles intentionally stay resumable; the regression contract
        # is that passive telemetry cannot silently move them to Watching.
        self.assertIsNotNone(server.get_continue_watching(self.db_path, self.user_id)["item"])

    def test_direct_player_action_clears_negative_feedback_but_heartbeat_does_not(self):
        server.update_user_state(
            10,
            {"not_interested": True},
            self.db_path,
            self.user_id,
        )

        passive = server.record_watch_event(
            self.watch_payload(event_type="heartbeat", engaged_seconds=10),
            self.db_path,
            self.user_id,
        )
        self.assertTrue(passive["state"]["not_interested"])

        resumed = server.record_watch_event(
            self.watch_payload(event_type="player_engaged"),
            self.db_path,
            self.user_id,
        )
        self.assertFalse(resumed["state"]["not_interested"])
        self.assertEqual(resumed["state"]["watch_status"], "watching")

    def test_meaningful_watch_signal_changes_only_at_threshold(self):
        server.record_watch_event(self.watch_payload(), self.db_path, self.user_id)
        before = server.record_watch_event(
            self.watch_payload(
                event_type="heartbeat",
                engaged_seconds=server.MEANINGFUL_WATCH_SECONDS - 1,
            ),
            self.db_path,
            self.user_id,
        )
        crossing = server.record_watch_event(
            self.watch_payload(event_type="heartbeat", engaged_seconds=1),
            self.db_path,
            self.user_id,
        )
        after = server.record_watch_event(
            self.watch_payload(event_type="heartbeat", engaged_seconds=1),
            self.db_path,
            self.user_id,
        )

        self.assertFalse(before["recommendation_signal_changed"])
        self.assertTrue(crossing["recommendation_signal_changed"])
        self.assertFalse(after["recommendation_signal_changed"])

    def test_progress_patch_persists_selected_source_and_rejects_wrong_episode(self):
        detail = self.detail()
        first_episode = detail["episodes"][0]
        con = server.connect(self.db_path)
        try:
            timestamp = server.now_iso()
            con.execute(
                """
                insert into video_sources(
                    anime_id, episode_id, provider_id, provider_title,
                    translation_id, translation_title, embed_host,
                    embed_url, embed_url_redacted, scraped_at
                ) values (10, ?, 'alt', 'Alt', 99, 'Alt Voice', 'alt.test', ?, ?, ?)
                """,
                (
                    first_episode["id"],
                    "https://alt.test/episode-1",
                    "https://alt.test/<redacted>",
                    timestamp,
                ),
            )
            selected_id = con.execute("select last_insert_rowid()").fetchone()[0]
            wrong_episode_source = con.execute(
                "select id from video_sources where episode_id = ? order by id limit 1",
                (detail["episodes"][1]["id"],),
            ).fetchone()[0]
            con.commit()
        finally:
            con.close()
        server.invalidate_catalog_cache(self.db_path)

        saved = server.update_user_state(
            10,
            {"progress_episode_number": 1, "video_source_id": selected_id},
            self.db_path,
            self.user_id,
        )
        self.assertEqual(saved["last_watch"]["video_source_id"], selected_id)
        con = server.connect(self.db_path)
        try:
            persisted = con.execute(
                """
                select video_source_id from user_episode_state
                where user_id = ? and progress_episode_number = 1
                """,
                (self.user_id,),
            ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(persisted, selected_id)

        with self.assertRaisesRegex(ValueError, "video_source_id is invalid"):
            server.update_user_state(
                10,
                {"progress_episode_number": 1, "video_source_id": wrong_episode_source},
                self.db_path,
                self.user_id,
            )

    def test_fresh_auto_migrate_adopts_runtime_equivalent_alter_migration(self):
        fresh_path = Path(self.tmpdir.name) / "fresh.sqlite"
        root = self.copy_library_migration()
        with patch.dict(
            os.environ,
            {
                "ANIME_AUTO_MIGRATE": "1",
                "ANIME_MIGRATIONS_ROOT": str(root),
                "ANIME_MIGRATION_NO_BACKUP": "1",
                "ANIME_MIGRATIONS_ROOTS": "",
            },
        ):
            server.prepare_database(fresh_path)
        con = sqlite3.connect(fresh_path)
        try:
            history = con.execute(
                f"select checksum_sha256 from {db_migrate.HISTORY_TABLE} where path = ?",
                (server.USER_LIBRARY_MIGRATION_PATH,),
            ).fetchone()
            columns = {row[1] for row in con.execute("pragma table_info(user_title_state)")}
        finally:
            con.close()
        self.assertEqual(history[0], db_migrate.file_checksum(LIBRARY_MIGRATION))
        self.assertIn("watch_status", columns)

    def test_later_auto_migrate_preserves_state_added_by_runtime_ensure(self):
        root = self.copy_library_migration()
        server.update_user_state(
            10,
            {"watch_status": "dropped", "not_interested": True},
            self.db_path,
            self.user_id,
        )
        with patch.dict(
            os.environ,
            {
                "ANIME_AUTO_MIGRATE": "1",
                "ANIME_MIGRATIONS_ROOT": str(root),
                "ANIME_MIGRATION_NO_BACKUP": "1",
                "ANIME_MIGRATIONS_ROOTS": "",
            },
        ):
            server.prepare_database(self.db_path)

        detail = self.detail()
        self.assertEqual(detail["watch_status"], "dropped")
        self.assertTrue(detail["not_interested"])
        con = sqlite3.connect(self.db_path)
        try:
            history_count = con.execute(
                f"select count(*) from {db_migrate.HISTORY_TABLE} where path = ?",
                (server.USER_LIBRARY_MIGRATION_PATH,),
            ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(history_count, 1)

    def test_direct_migration_runner_adopts_schema_after_runtime_boot(self):
        root = self.copy_library_migration()
        server.update_user_state(
            10,
            {"watch_status": "dropped", "not_interested": True},
            self.db_path,
            self.user_id,
        )

        result = db_migrate.apply_pending(self.db_path, root, no_backup=True)

        self.assertEqual(result["applied"], [])
        self.assertEqual(
            [migration.path for migration in result["adopted"]],
            [server.USER_LIBRARY_MIGRATION_PATH],
        )
        detail = self.detail()
        self.assertEqual(detail["watch_status"], "dropped")
        self.assertTrue(detail["not_interested"])

    def test_direct_runner_does_not_adopt_spoofed_runtime_contract(self):
        root = Path(self.tmpdir.name) / "spoofed-migrations"
        target = root / LIBRARY_MIGRATION.parent.name / LIBRARY_MIGRATION.name
        target.parent.mkdir(parents=True)
        target.write_text(
            "-- runtime-schema-contract: user-library-state-v1\n"
            "create table spoofed_contract_was_executed(id integer);\n",
            encoding="utf-8",
        )

        result = db_migrate.apply_pending(self.db_path, root, no_backup=True)

        self.assertEqual(result["adopted"], [])
        self.assertEqual(len(result["applied"]), 1)
        con = sqlite3.connect(self.db_path)
        try:
            table = con.execute(
                "select 1 from sqlite_master "
                "where type = 'table' and name = 'spoofed_contract_was_executed'"
            ).fetchone()
        finally:
            con.close()
        self.assertIsNotNone(table)

    def test_adoption_backup_precedes_history_mutation(self):
        root = self.copy_library_migration()
        backup_dir = Path(self.tmpdir.name) / "migration-backups"

        result = db_migrate.apply_pending(
            self.db_path,
            root,
            backup_dir=backup_dir,
        )

        self.assertIsNotNone(result["backup"])
        backup = sqlite3.connect(result["backup"])
        try:
            has_history = backup.execute(
                "select 1 from sqlite_master where type = 'table' and name = ?",
                (db_migrate.HISTORY_TABLE,),
            ).fetchone()
            history_count = 0 if not has_history else backup.execute(
                f"select count(*) from {db_migrate.HISTORY_TABLE} where path = ?",
                (server.USER_LIBRARY_MIGRATION_PATH,),
            ).fetchone()[0]
        finally:
            backup.close()
        self.assertEqual(history_count, 0)


if __name__ == "__main__":
    unittest.main()
