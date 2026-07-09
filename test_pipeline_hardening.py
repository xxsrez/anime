#!/usr/bin/env python3
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import MagicMock, call, patch
import urllib.error

import scrape_animego
import server
import sync_videos
import update_backup
from scripts import check_data_health
from scripts import check_repo_hygiene
from scripts import http_safety
from scripts import missing_db_bootstrap
from scripts import railway_daily_sync
from scripts.atomic_publish import atomic_publish_directory
from scripts.operation_lock import DatabaseOperationLock, OperationLockError, default_lock_path


class PipelineHardeningTest(unittest.TestCase):
    def test_http_retry_closes_http_error_response_before_propagating(self):
        response = MagicMock()
        http_error = urllib.error.HTTPError(
            "https://animego.me/failing",
            500,
            "Internal Server Error",
            {},
            response,
        )
        args = SimpleNamespace(retry_attempts=1, retry_backoff=0)

        def fail():
            raise http_error

        with self.assertRaises(urllib.error.HTTPError):
            sync_videos.call_with_retries(args, "failing request", fail)

        response.close.assert_called_once_with()

    def test_repo_hygiene_checks_publishable_history_not_codex_internal_refs(self):
        with patch.object(check_repo_hygiene, "git", return_value=[]) as git_mock:
            self.assertEqual(check_repo_hygiene.check_history_paths(), [])

        git_mock.assert_called_once_with(
            "log",
            "--branches",
            "--remotes",
            "--tags",
            "--name-only",
            "--pretty=format:",
        )

    def test_repo_hygiene_includes_untracked_candidate_files(self):
        with patch.object(check_repo_hygiene, "git", return_value=[]) as git_mock:
            files, errors = check_repo_hygiene.check_tracked_files()

        self.assertEqual(files, [])
        self.assertEqual(errors, [])
        git_mock.assert_called_once_with(
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
        )

    def test_default_operation_lock_path_collapses_symlink_aliases(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "anime.sqlite"
            alias_path = Path(tmpdir) / "anime-alias.sqlite"
            db_path.touch()
            alias_path.symlink_to(db_path)

            self.assertEqual(default_lock_path(db_path), default_lock_path(alias_path))

    def test_operation_lock_closes_handle_when_entry_setup_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            handle = MagicMock()
            handle.write.side_effect = OSError("simulated lock metadata failure")
            lock = DatabaseOperationLock(
                Path(tmpdir) / "anime.sqlite",
                path=Path(tmpdir) / "operation.lock",
            )
            with (
                patch("scripts.operation_lock.Path.open", return_value=handle),
                patch("scripts.operation_lock.fcntl.flock"),
                self.assertRaisesRegex(OSError, "simulated lock metadata failure"),
            ):
                lock.__enter__()

            handle.close.assert_called_once_with()
            self.assertIsNone(lock.handle)

    def test_atomic_publish_directory_replaces_target_and_cleans_staging(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "current"
            target.mkdir()
            (target / "old.txt").write_text("old", encoding="utf-8")

            with atomic_publish_directory(
                target,
                stage_prefix=".current.stage-",
                previous_prefix=".current.previous-",
            ) as stage:
                (stage / "new.txt").write_text("new", encoding="utf-8")
                self.assertEqual((target / "old.txt").read_text(encoding="utf-8"), "old")

            self.assertEqual((target / "new.txt").read_text(encoding="utf-8"), "new")
            self.assertFalse((target / "old.txt").exists())
            self.assertEqual(list(root.glob(".current.stage-*")), [])
            self.assertEqual(list(root.glob(".current.previous-*")), [])

    def test_atomic_publish_directory_restores_target_when_publication_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "current"
            target.mkdir()
            sentinel = target / "sentinel.txt"
            sentinel.write_text("last known good", encoding="utf-8")
            real_replace = os.replace
            paths = {}

            def fail_stage_publication(source, destination):
                if (
                    Path(source) == paths.get("stage")
                    and Path(destination) == paths.get("target")
                ):
                    raise OSError("simulated publication failure")
                return real_replace(source, destination)

            with patch("scripts.atomic_publish.os.replace", side_effect=fail_stage_publication):
                with self.assertRaisesRegex(OSError, "simulated publication failure"):
                    with atomic_publish_directory(
                        target,
                        stage_prefix=".current.stage-",
                        previous_prefix=".current.previous-",
                    ) as stage:
                        paths["stage"] = stage
                        paths["target"] = stage.parent / target.name
                        (stage / "new.txt").write_text("new", encoding="utf-8")

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "last known good")
            self.assertFalse((target / "new.txt").exists())
            self.assertEqual(list(root.glob(".current.stage-*")), [])
            self.assertEqual(list(root.glob(".current.previous-*")), [])

    def test_atomic_publish_directory_wait_timeout_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "current"
            with atomic_publish_directory(target):
                started = time.monotonic()
                with self.assertRaises(OperationLockError):
                    with atomic_publish_directory(target, wait=True, timeout=0.05):
                        pass
                self.assertLess(time.monotonic() - started, 0.5)

    def test_atomic_publish_preserves_recovery_if_competing_target_blocks_rollback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "current"
            target.mkdir()
            (target / "sentinel.txt").write_text("recover me", encoding="utf-8")
            real_replace = os.replace
            paths = {}

            def replace_with_competitor(source, destination):
                if (
                    Path(source) == paths.get("stage")
                    and Path(destination) == paths.get("target")
                ):
                    paths["target"].mkdir()
                    (paths["target"] / "competitor.txt").write_text(
                        "new owner", encoding="utf-8"
                    )
                    raise OSError("simulated competing publication")
                return real_replace(source, destination)

            with patch("scripts.atomic_publish.os.replace", side_effect=replace_with_competitor):
                with self.assertRaisesRegex(OSError, "simulated competing publication"):
                    with atomic_publish_directory(
                        target,
                        stage_prefix=".current.stage-",
                        previous_prefix=".current.previous-",
                    ) as stage:
                        paths["stage"] = stage
                        paths["target"] = stage.parent / target.name
                        (stage / "new.txt").write_text("new", encoding="utf-8")

            recovery_dirs = list(root.glob(".current.previous-*"))
            self.assertEqual(len(recovery_dirs), 1)
            self.assertEqual(
                (recovery_dirs[0] / "sentinel.txt").read_text(encoding="utf-8"),
                "recover me",
            )
            self.assertEqual((target / "competitor.txt").read_text(encoding="utf-8"), "new owner")
            self.assertEqual(list(root.glob(".current.stage-*")), [])

    def test_concurrent_migration_publishers_serialize_overwrite_check(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "migrations"
            folder = "2026-07-09_concurrent"
            filename = "00_data.sql"
            first_build_started = threading.Event()
            release_first = threading.Event()
            second_build_entered = threading.Event()
            first_paths = []
            first_errors = []
            second_errors = []

            def first_statements():
                yield "insert into x values (1);"
                first_build_started.set()
                if not release_first.wait(5):
                    raise TimeoutError("test did not release first publisher")
                yield "insert into x values (2);"

            def second_statements():
                yield "insert into x values (999);"
                second_build_entered.set()

            def publish_first():
                try:
                    first_paths.extend(
                        sync_videos.write_migration_files(
                            root,
                            folder,
                            filename,
                            "-- first\n",
                            first_statements(),
                        )
                    )
                except Exception as exc:  # pragma: no cover - asserted below
                    first_errors.append(exc)

            def publish_second():
                try:
                    sync_videos.write_migration_files(
                        root,
                        folder,
                        filename,
                        "-- second\n",
                        second_statements(),
                        publish_wait=True,
                        publish_timeout=2.0,
                    )
                except Exception as exc:
                    second_errors.append(exc)

            first_thread = threading.Thread(target=publish_first, daemon=True)
            second_thread = threading.Thread(target=publish_second, daemon=True)
            first_thread.start()
            self.assertTrue(first_build_started.wait(2))
            try:
                started = time.monotonic()
                with self.assertRaises(OperationLockError):
                    sync_videos.write_migration_files(
                        root,
                        folder,
                        filename,
                        "-- fail fast\n",
                        ["insert into x values (998);"],
                    )
                self.assertLess(time.monotonic() - started, 0.5)

                second_thread.start()
                time.sleep(0.05)
                self.assertTrue(second_thread.is_alive())
                self.assertFalse(second_build_entered.is_set())
            finally:
                release_first.set()

            first_thread.join(2)
            second_thread.join(2)
            self.assertFalse(first_thread.is_alive())
            self.assertFalse(second_thread.is_alive())
            self.assertEqual(first_errors, [])
            self.assertEqual(len(second_errors), 1)
            self.assertIsInstance(second_errors[0], FileExistsError)
            self.assertFalse(second_build_entered.is_set())
            self.assertEqual([path.name for path in first_paths], [filename])
            published_sql = (root / folder / filename).read_text(encoding="utf-8")
            self.assertIn("values (1)", published_sql)
            self.assertIn("values (2)", published_sql)
            self.assertNotIn("values (999)", published_sql)

    def test_http_safety_caches_only_the_unchanged_current_origin(self):
        response = MagicMock()
        response.geturl.return_value = "https://animego.me/anime/final"
        opener = MagicMock()
        opener.open.return_value = response
        with patch("scripts.http_safety.public_host_addresses", return_value=True) as resolve:
            with patch("scripts.http_safety.build_opener", return_value=opener):
                returned = http_safety.open_validated_url(
                    "https://animego.me/anime/start",
                    timeout=1,
                    allowed_hosts=("animego.me",),
                )
        self.assertIs(returned, response)
        resolve.assert_called_once_with("animego.me", 443)

        redirected = MagicMock()
        redirected.geturl.return_value = "https://media.animego.me/anime/final"
        opener.open.return_value = redirected
        with patch("scripts.http_safety.public_host_addresses", return_value=True) as resolve:
            with patch("scripts.http_safety.build_opener", return_value=opener):
                http_safety.open_validated_url(
                    "https://animego.me/anime/start",
                    timeout=1,
                    allowed_hosts=("animego.me",),
                )
        self.assertEqual(
            resolve.call_args_list,
            [call("animego.me", 443), call("media.animego.me", 443)],
        )

        credentialed = MagicMock()
        credentialed.geturl.return_value = "https://user:secret@animego.me/anime/final"
        opener.open.return_value = credentialed
        with patch("scripts.http_safety.public_host_addresses", return_value=True) as resolve:
            with patch("scripts.http_safety.build_opener", return_value=opener):
                with self.assertRaisesRegex(ValueError, "credentials"):
                    http_safety.open_validated_url(
                        "https://animego.me/anime/start",
                        timeout=1,
                        allowed_hosts=("animego.me",),
                    )
        resolve.assert_called_once_with("animego.me", 443)
        credentialed.close.assert_called_once_with()

    def test_http_safety_rejects_private_resolution_redirects_and_final_urls(self):
        private_resolution = [(2, 1, 6, "", ("127.0.0.1", 443))]
        with patch("scripts.http_safety.socket.getaddrinfo", return_value=private_resolution):
            with self.assertRaisesRegex(ValueError, "non-public"):
                http_safety.validate_http_url(
                    "https://animego.me/anime/test",
                    allowed_hosts=("animego.me",),
                )

        redirect_validator = MagicMock(side_effect=ValueError("redirect rejected"))
        handler = http_safety.ValidatingRedirectHandler(redirect_validator)
        with self.assertRaisesRegex(ValueError, "redirect rejected"):
            handler.redirect_request(
                None,
                None,
                302,
                "Found",
                {},
                "http://127.0.0.1/private",
            )
        redirect_validator.assert_called_once_with("http://127.0.0.1/private")

        response = MagicMock()
        response.geturl.return_value = "http://127.0.0.1/private"
        opener = MagicMock()
        opener.open.return_value = response
        with patch("scripts.http_safety.public_host_addresses", return_value=True):
            with patch("scripts.http_safety.build_opener", return_value=opener):
                with self.assertRaisesRegex(ValueError, "HTTPS URL required"):
                    http_safety.open_validated_url(
                        "https://animego.me/anime/test",
                        timeout=1,
                        allowed_hosts=("animego.me",),
                    )
        response.close.assert_called_once_with()

    def test_operation_lock_wait_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "anime.sqlite"
            lock_path = Path(tmpdir) / "operation.lock"
            with DatabaseOperationLock(db_path, path=lock_path, operation="first"):
                started = time.monotonic()
                with self.assertRaises(OperationLockError):
                    with DatabaseOperationLock(
                        db_path,
                        path=lock_path,
                        wait=True,
                        timeout=0.05,
                        poll_interval=0.01,
                        operation="second",
                    ):
                        pass
                self.assertLess(time.monotonic() - started, 0.5)

    def test_failed_sync_is_partial_nonzero_and_does_not_advance_last_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "anime.sqlite"
            scrape_animego.init_db(db_path).close()
            args = sync_videos.parse_args(
                [
                    "--db",
                    str(db_path),
                    "--mode",
                    "manual",
                    "--yummy-ref",
                    "dummy-title",
                    "--delay",
                    "0",
                ]
            )

            with patch(
                "sync_videos.sync_yummyanime",
                return_value={"failed": 1, "titles_checked": 1},
            ):
                with self.assertRaises(sync_videos.SyncFailedError):
                    sync_videos.run_sync(args)

            con = sqlite3.connect(db_path)
            try:
                state = dict(con.execute("select key, value from video_sync_state"))
                self.assertEqual(state["yummyanime:manual:last_status"], "failed")
                self.assertNotIn("yummyanime:manual:last_success", state)
                self.assertEqual(
                    con.execute("select status from content_update_runs order by id desc").fetchone()[0],
                    "partial",
                )
            finally:
                con.close()

    def test_ongoing_feed_stops_when_full_page_repeats(self):
        rows = [{"anime_url": f"https://ru.yummyani.me/catalog/item/title-{index}"} for index in range(100)]
        args = SimpleNamespace(
            yummy_ongoing_max_pages=20,
            yummy_limit=0,
            episode_limit=0,
            dry_run=False,
            stop_on_error=False,
            verbose=False,
        )
        stats = {"ongoing_failed": 0, "failed": 0}
        con = sqlite3.connect(":memory:")
        with patch("sync_videos.fetch_json_url", side_effect=[{"response": rows}, {"response": rows}]) as fetch:
            with patch("sync_videos.sync_yummy_title"):
                with patch("builtins.print"):
                    sync_videos.sync_yummy_ongoing(con, args, stats)
        con.close()

        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(stats["failed"], 1)

    def test_manual_animego_ref_preserves_listing_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "anime.sqlite"
            con = scrape_animego.init_db(db_path)
            con.execute(
                """
                insert into anime(
                    id, slug, title, subtitle, url, cover_url, source, source_id,
                    listing_score, kind, year, description, scraped_at
                ) values (42, 'old', 'Title', 'Subtitle', 'https://animego.me/anime/old-42',
                          'https://img.test/42.jpg', 'animego', '42', 8.8, 'TV', '2024',
                          'Description', '2026-07-09T00:00:00+00:00')
                """
            )
            con.commit()

            item = sync_videos.animego_item_from_ref("https://animego.me/anime/new-slug-42", con)
            self.assertEqual(item["subtitle"], "Subtitle")
            self.assertEqual(item["listing_score"], 8.8)
            self.assertEqual(item["year"], "2024")
            self.assertEqual(item["cover_url"], "https://img.test/42.jpg")
            con.close()

    def test_authoritative_metadata_can_clear_children_while_partial_preserves_them(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            con = scrape_animego.init_db(Path(tmpdir) / "anime.sqlite")
            item = {
                "id": 77,
                "slug": "metadata-title",
                "title": "Metadata Title",
                "subtitle": "Original subtitle",
                "url": "https://animego.me/anime/metadata-title-77",
                "cover_url": "https://img.test/77.jpg",
                "source": "animego",
                "source_id": "77",
                "listing_score": 8.5,
                "kind": "TV",
                "year": "2024",
                "genres": ["Action"],
                "listing_description": "Original description",
            }
            detail = {
                "title": "Metadata Title",
                "cover_url": "https://img.test/77.jpg",
                "aggregate_score": 8.0,
                "aggregate_count": 10,
                "date_published": "2024-01-01",
                "content_rating": "PG-13",
                "fields": {"Статус": "Онгоинг", "Студия": "Test Studio"},
                "genres": ["Action"],
                "dubbings": ["Test Voice"],
                "description": "Original description",
                "schema_data": {"name": "Metadata Title"},
            }
            scrape_animego.upsert_anime(
                con, item, detail, "2026-07-09T00:00:00+00:00", authoritative_metadata=True
            )
            partial_item = {
                **item,
                "subtitle": None,
                "cover_url": None,
                "listing_score": None,
                "kind": None,
                "year": None,
                "genres": [],
                "listing_description": None,
            }
            partial_detail = {
                **detail,
                "cover_url": None,
                "aggregate_score": None,
                "aggregate_count": None,
                "date_published": None,
                "content_rating": None,
                "fields": {},
                "genres": [],
                "dubbings": [],
                "description": None,
                "schema_data": {},
            }
            scrape_animego.upsert_anime(
                con,
                partial_item,
                partial_detail,
                "2026-07-09T01:00:00+00:00",
                authoritative_metadata=False,
            )
            preserved = con.execute(
                "select subtitle, year, status, fields_json, schema_json from anime where id=77"
            ).fetchone()
            self.assertEqual(preserved[:3], ("Original subtitle", "2024", "Онгоинг"))
            self.assertEqual(json.loads(preserved[3]), detail["fields"])
            self.assertEqual(json.loads(preserved[4]), detail["schema_data"])
            self.assertEqual(con.execute("select count(*) from anime_genres where anime_id=77").fetchone()[0], 1)

            traced_statements = []
            con.set_trace_callback(traced_statements.append)
            original_json_dumps = json.dumps
            with patch.object(scrape_animego.json, "dumps", side_effect=original_json_dumps) as dumps:
                scrape_animego.upsert_anime(
                    con,
                    partial_item,
                    partial_detail,
                    "2026-07-09T02:00:00+00:00",
                    authoritative_metadata=True,
                )
            con.set_trace_callback(None)
            anime_writes = [
                statement
                for statement in traced_statements
                if " ".join(statement.lower().split()).startswith(
                    ("insert into anime ", "update anime ")
                )
            ]
            self.assertEqual(len(anime_writes), 1)
            self.assertEqual(dumps.call_count, 2)
            cleared = con.execute(
                "select subtitle, year, status, fields_json, schema_json from anime where id=77"
            ).fetchone()
            self.assertEqual(cleared[:3], (None, None, None))
            self.assertEqual(json.loads(cleared[3]), {})
            self.assertEqual(json.loads(cleared[4]), {})
            self.assertEqual(con.execute("select count(*) from anime_fields where anime_id=77").fetchone()[0], 0)
            self.assertEqual(con.execute("select count(*) from anime_genres where anime_id=77").fetchone()[0], 0)
            self.assertEqual(con.execute("select count(*) from anime_dubbings where anime_id=77").fetchone()[0], 0)
            con.close()

    def test_kodik_redaction_covers_all_known_route_variants(self):
        for route in ("seria/1", "serial/2", "season/3", "video/4"):
            redacted = scrape_animego.redact_embed_url(
                f"https://kodikplayer.com/{route}/secret-token?hash=secret"
            )
            self.assertNotIn("secret-token", redacted)
            self.assertNotIn("hash=secret", redacted)
            self.assertIn("redacted", redacted)
        credential_url = "https://user:secret@kodikplayer.com/video/4/token"
        self.assertIsNone(scrape_animego.redact_embed_url(credential_url))
        self.assertIsNone(scrape_animego.embed_host(credential_url))

    def test_overwriting_shorter_migration_bundle_removes_stale_chunks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "migrations"
            folder = "2026-07-09_generated"
            filename = "00_data.sql"
            first = sync_videos.write_migration_files(
                root,
                folder,
                filename,
                "-- generated\n",
                [f"insert into x values ({index});" for index in range(20)],
                max_bytes=80,
            )
            self.assertGreater(len(first), 1)

            second = sync_videos.write_migration_files(
                root,
                folder,
                filename,
                "-- generated\n",
                ["insert into x values (1);"],
                overwrite=True,
                max_bytes=10_000,
            )

            self.assertEqual([path.name for path in second], [filename])
            self.assertEqual(
                sorted(path.name for path in (root / folder).glob("*.sql")),
                [filename],
            )

    def make_backup_fixture(self, db_path):
        con = server.connect(db_path)
        now = server.now_iso()
        con.execute(
            """
            insert into users(google_sub, email, email_verified, name, created_at)
            values ('backup-user', 'backup@example.test', 1, 'Backup User', ?)
            """,
            (now,),
        )
        user_id = con.execute("select id from users where google_sub='backup-user'").fetchone()[0]
        item = {
            "id": 101,
            "slug": "backup-title",
            "title": "Backup Title",
            "subtitle": None,
            "url": "https://animego.me/anime/backup-title-101",
            "cover_url": None,
            "source": "animego",
            "source_id": "101",
            "listing_score": None,
            "kind": "TV",
            "year": "2026",
            "genres": [],
            "listing_description": None,
        }
        detail = {
            "title": "Backup Title",
            "cover_url": None,
            "aggregate_score": None,
            "aggregate_count": None,
            "date_published": None,
            "content_rating": None,
            "fields": {},
            "genres": [],
            "dubbings": [],
            "description": None,
            "schema_data": {},
        }
        episode = {
            "id": 101001,
            "number": "1",
            "title": "Episode 1",
            "release_label": None,
            "episode_type": "episode",
            "description": None,
        }
        provider = {
            "provider_id": "backup-provider",
            "provider_title": "Backup Provider",
            "translation_id": 701,
            "translation_title": "Backup Voice",
            "embed_host": "example.test",
            "embed_url": "https://example.test/embed/101",
            "embed_url_redacted": "//example.test/embed/<redacted>",
        }
        scrape_animego.upsert_anime(con, item, detail, now, authoritative_metadata=True)
        scrape_animego.upsert_episode(con, 101, episode, True, None, now)
        scrape_animego.upsert_provider(con, 101, 101001, provider, True, now)
        con.execute(
            """
            insert into user_title_state(
                user_id, anime_id, is_favorite, progress_episode_number, watched, updated_at
            ) values (?, 101, 1, 1, 1, ?)
            """,
            (user_id, now),
        )
        video_source_id = con.execute(
            "select id from video_sources where anime_id=101 order by id limit 1"
        ).fetchone()[0]
        con.commit()
        con.close()
        server.record_watch_event(
            {
                "client_session_id": "backup-watch-session",
                "event_type": "heartbeat",
                "anime_id": 101,
                "episode_id": 101001,
                "episode_number": "1",
                "progress_episode_number": 1,
                "video_source_id": video_source_id,
                "source": "animego",
                "source_anime_id": 101,
                "provider_id": "backup-provider",
                "provider_title": "Backup Provider",
                "embed_host": "example.test",
                "engaged_seconds": 45,
                "page_visible": True,
                "player_focused": True,
            },
            db_path,
            user_id=user_id,
        )
        return user_id

    def test_backup_bundle_is_valid_and_state_sql_restores_existing_table(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "anime.sqlite"
            out_dir = Path(tmpdir) / "current"
            lock_path = Path(tmpdir) / "backup.operation.lock"
            user_id = self.make_backup_fixture(db_path)
            args = SimpleNamespace(
                db=str(db_path),
                out=str(out_dir),
                lock_file=str(lock_path),
                wait_lock=False,
            )

            validate_snapshot = update_backup.validate_snapshot

            def validate_after_snapshot_lock(snapshot_path):
                with DatabaseOperationLock(
                    db_path,
                    path=lock_path,
                    operation="backup lock scope regression",
                ):
                    pass
                return validate_snapshot(snapshot_path)

            with patch(
                "update_backup.validate_snapshot",
                side_effect=validate_after_snapshot_lock,
            ) as validate:
                update_backup.update_backup(args)
            self.assertEqual(validate.call_count, 1)
            check_data_health.verify_checksums(out_dir / "SHA256SUMS")
            update_backup.validate_snapshot(out_dir / "animego.sqlite")

            con = sqlite3.connect(db_path)
            expected_title_state = con.execute(
                "select * from user_title_state where user_id=? and anime_id=101",
                (user_id,),
            ).fetchone()
            expected_episode_states = con.execute(
                "select * from user_episode_state order by user_id, anime_id, episode_id"
            ).fetchall()
            expected_watch_events = con.execute(
                "select * from user_watch_events order by id"
            ).fetchall()
            con.execute(
                "update user_title_state set is_favorite=0, watched=0 where user_id=? and anime_id=101",
                (user_id,),
            )
            con.execute("delete from user_watch_events")
            con.execute("delete from user_episode_state")
            con.commit()
            con.executescript((out_dir / "user_state.sql").read_text(encoding="utf-8"))
            restored = con.execute(
                "select * from user_title_state where user_id=? and anime_id=101",
                (user_id,),
            ).fetchone()
            restored_episode_states = con.execute(
                "select * from user_episode_state order by user_id, anime_id, episode_id"
            ).fetchall()
            restored_watch_events = con.execute(
                "select * from user_watch_events order by id"
            ).fetchall()
            con.close()
            self.assertEqual(restored, expected_title_state)
            self.assertEqual(restored_episode_states, expected_episode_states)
            self.assertEqual(restored_watch_events, expected_watch_events)

            exported = json.loads((out_dir / "user_title_state.json").read_text(encoding="utf-8"))
            self.assertEqual(exported[0]["user_id"], user_id)

    def test_failed_backup_keeps_previous_published_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "anime.sqlite"
            out_dir = Path(tmpdir) / "current"
            self.make_backup_fixture(db_path)
            out_dir.mkdir()
            sentinel = out_dir / "sentinel.txt"
            sentinel.write_text("last known good", encoding="utf-8")
            args = SimpleNamespace(
                db=str(db_path),
                out=str(out_dir),
                lock_file=None,
                wait_lock=False,
            )

            with patch("update_backup.validate_snapshot", side_effect=RuntimeError("bad snapshot")):
                with self.assertRaises(RuntimeError):
                    update_backup.update_backup(args)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "last known good")

    def test_backup_rejects_output_that_is_or_contains_active_database(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "anime.sqlite"
            self.make_backup_fixture(db_path)
            original_header = db_path.read_bytes()[:16]

            for out_path in (db_path, Path(tmpdir)):
                args = SimpleNamespace(
                    db=str(db_path),
                    out=str(out_path),
                    lock_file=None,
                    wait_lock=False,
                )
                with self.assertRaises(ValueError):
                    update_backup.update_backup(args)

            self.assertTrue(db_path.is_file())
            self.assertEqual(db_path.read_bytes()[:16], original_header)

    def test_missing_database_bootstrap_detects_upload_and_requests_handoff(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "anime.sqlite"
            self.assertFalse(missing_db_bootstrap.database_ready(db_path))
            db_path.write_bytes(b"not sqlite")
            self.assertFalse(missing_db_bootstrap.database_ready(db_path))
            db_path.unlink()

            shutdown_called = threading.Event()

            class FakeServer:
                def shutdown(self):
                    shutdown_called.set()

            watcher = threading.Thread(
                target=missing_db_bootstrap.wait_for_database,
                args=(FakeServer(), db_path, 0.01),
                daemon=True,
            )
            watcher.start()
            con = sqlite3.connect(db_path)
            con.executescript(
                """
                create table anime(id integer primary key);
                create table episodes(id integer primary key);
                create table video_sources(id integer primary key);
                """
            )
            con.commit()
            con.close()

            self.assertTrue(shutdown_called.wait(2))
            watcher.join(timeout=2)
            self.assertTrue(missing_db_bootstrap.database_ready(db_path))

    def test_railway_cron_rejects_partial_success_payload(self):
        self.assertFalse(
            railway_daily_sync.sync_succeeded(
                {"ok": True, "result": {"animego": {"failed": 1}}}
            )
        )
        self.assertTrue(
            railway_daily_sync.sync_succeeded(
                {"ok": True, "result": {"animego": {"failed": 0}}}
            )
        )
        self.assertEqual(
            railway_daily_sync.target_url(
                "https://anime.example/api/internal/daily-sync?mode=hourly&source=cron",
                "daily",
            ),
            "https://anime.example/api/internal/daily-sync?source=cron&mode=daily",
        )


if __name__ == "__main__":
    unittest.main()
