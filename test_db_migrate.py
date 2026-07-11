#!/usr/bin/env python3
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
import scrape_animego
import sync_videos
from scripts import db_data_diff
from scripts import db_migrate


class DatabaseMigrationTest(unittest.TestCase):
    def create_db(self, path):
        con = sqlite3.connect(path)
        con.execute("create table migration_log (id integer primary key autoincrement, label text not null)")
        con.commit()
        con.close()

    def write_migration(self, root, folder, filename, sql):
        path = Path(root) / folder
        path.mkdir(parents=True, exist_ok=True)
        script = path / filename
        script.write_text(sql, encoding="utf-8")
        return script

    def labels(self, db_path):
        con = sqlite3.connect(db_path)
        try:
            return [row[0] for row in con.execute("select label from migration_log order by id")]
        finally:
            con.close()

    def history_paths(self, db_path):
        con = sqlite3.connect(db_path)
        try:
            if not db_migrate.history_table_exists(con):
                return []
            return [
                row[0]
                for row in con.execute(
                    f"select path from {db_migrate.HISTORY_TABLE} order by path"
                )
            ]
        finally:
            con.close()

    def test_library_contract_adoption_does_not_timestamp_neutral_none(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript(
            """
            create table user_title_state (
                user_id integer not null,
                anime_id integer not null,
                is_favorite integer not null default 0,
                progress_episode_number integer,
                watched integer not null default 0,
                watch_status text not null default 'none',
                not_interested integer not null default 0,
                updated_at text not null,
                favorite_updated_at text,
                watch_status_updated_at text,
                not_interested_updated_at text,
                primary key(user_id, anime_id)
            );
            create index idx_user_title_state_user_watch_status
                on user_title_state(user_id, watch_status, watch_status_updated_at desc);
            create index idx_user_title_state_user_not_interested
                on user_title_state(user_id, not_interested) where not_interested = 1;
            insert into user_title_state values
                (1, 10, 0, 4, 1, 'completed', 0, '2026-01-01T00:00:00+00:00',
                 null, '2026-01-01T00:00:00+00:00', null),
                (1, 11, 1, null, 0, 'none', 0, '2026-02-01T00:00:00+00:00',
                 '2026-02-01T00:00:00+00:00', null, null);
            """
        )

        self.assertTrue(db_migrate.user_library_state_v1_satisfied(con))
        rows = con.execute(
            "select * from user_title_state order by anime_id"
        ).fetchall()
        con.close()

        self.assertIsNone(rows[1]["watch_status_updated_at"])
        aggregate = server.aggregate_state_rows(rows)
        self.assertTrue(aggregate["is_favorite"])
        self.assertEqual(aggregate["watch_status"], "completed")

    def test_applies_folders_and_files_in_lexicographic_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "migrations"
            db_path = Path(tmpdir) / "anime.sqlite"
            self.create_db(db_path)
            self.write_migration(root, "2016-01-02_second", "01_second.sql", "insert into migration_log(label) values ('second-01');")
            self.write_migration(root, "2016-01-02_second", "00_first.sql", "insert into migration_log(label) values ('second-00');")
            self.write_migration(root, "2015-12-30_first", "00_first.sql", "insert into migration_log(label) values ('first-00');")

            result = db_migrate.apply_pending(db_path, root, no_backup=True, verify=True)

            self.assertEqual(
                [migration.path for migration, _ in result["applied"]],
                [
                    "2015-12-30_first/00_first.sql",
                    "2016-01-02_second/00_first.sql",
                    "2016-01-02_second/01_second.sql",
                ],
            )
            self.assertEqual(self.labels(db_path), ["first-00", "second-00", "second-01"])

    def test_applies_multiple_roots_in_cli_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            public_root = Path(tmpdir) / "migrations"
            private_root = Path(tmpdir) / "private-migrations"
            db_path = Path(tmpdir) / "anime.sqlite"
            self.create_db(db_path)
            self.write_migration(public_root, "2026-07-08_public", "00_public.sql", "insert into migration_log(label) values ('public');")
            self.write_migration(private_root, "2020-01-01_private", "00_private.sql", "insert into migration_log(label) values ('private');")

            result = db_migrate.apply_pending(db_path, [public_root, private_root], no_backup=True)

            self.assertEqual(
                [migration.path for migration, _ in result["applied"]],
                [
                    "2026-07-08_public/00_public.sql",
                    "2020-01-01_private/00_private.sql",
                ],
            )
            self.assertEqual(self.labels(db_path), ["public", "private"])

    def test_duplicate_relative_path_across_roots_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            public_root = Path(tmpdir) / "migrations"
            private_root = Path(tmpdir) / "private-migrations"
            db_path = Path(tmpdir) / "anime.sqlite"
            self.create_db(db_path)
            self.write_migration(public_root, "2026-07-08_same", "00_same.sql", "insert into migration_log(label) values ('public');")
            self.write_migration(private_root, "2026-07-08_same", "00_same.sql", "insert into migration_log(label) values ('private');")

            with self.assertRaises(db_migrate.MigrationError):
                db_migrate.apply_pending(db_path, [public_root, private_root], no_backup=True)

            self.assertEqual(self.labels(db_path), [])

    def test_second_apply_skips_already_applied_scripts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "migrations"
            db_path = Path(tmpdir) / "anime.sqlite"
            self.create_db(db_path)
            self.write_migration(root, "2026-07-08_static-data", "00_seed.sql", "insert into migration_log(label) values ('seed');")

            first = db_migrate.apply_pending(db_path, root, no_backup=True)
            second = db_migrate.apply_pending(db_path, root, no_backup=True)

            self.assertEqual(len(first["applied"]), 1)
            self.assertEqual(second["applied"], [])
            self.assertEqual(self.labels(db_path), ["seed"])
            self.assertEqual(self.history_paths(db_path), ["2026-07-08_static-data/00_seed.sql"])

    def test_checksum_drift_fails_before_applying_new_scripts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "migrations"
            db_path = Path(tmpdir) / "anime.sqlite"
            self.create_db(db_path)
            script = self.write_migration(root, "2026-07-08_fix", "00_fix.sql", "insert into migration_log(label) values ('old');")
            db_migrate.apply_pending(db_path, root, no_backup=True)
            script.write_text("insert into migration_log(label) values ('changed');", encoding="utf-8")
            self.write_migration(root, "2026-07-09_new", "00_new.sql", "insert into migration_log(label) values ('new');")

            with self.assertRaises(db_migrate.MigrationDriftError):
                db_migrate.apply_pending(db_path, root, no_backup=True)

            self.assertEqual(self.labels(db_path), ["old"])

    def test_failed_migration_rolls_back_file_and_history_row(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "migrations"
            db_path = Path(tmpdir) / "anime.sqlite"
            self.create_db(db_path)
            self.write_migration(
                root,
                "2026-07-08_bad",
                "00_bad.sql",
                """
                create table should_rollback (value text);
                insert into should_rollback(value) values ('created');
                select missing_column from missing_table;
                """,
            )

            with self.assertRaises(sqlite3.Error):
                db_migrate.apply_pending(db_path, root, no_backup=True)

            con = sqlite3.connect(db_path)
            try:
                self.assertIsNone(
                    con.execute(
                        "select 1 from sqlite_master where type = 'table' and name = 'should_rollback'"
                    ).fetchone()
                )
                self.assertFalse(db_migrate.history_table_exists(con))
            finally:
                con.close()

    def test_pending_batch_is_atomic_across_multiple_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "migrations"
            db_path = Path(tmpdir) / "anime.sqlite"
            self.create_db(db_path)
            self.write_migration(
                root,
                "2026-07-08_batch",
                "00_first.sql",
                "insert into migration_log(label) values ('must-rollback');",
            )
            self.write_migration(
                root,
                "2026-07-08_batch",
                "01_bad.sql",
                "select missing_column from missing_table;",
            )

            with self.assertRaises(sqlite3.Error):
                db_migrate.apply_pending(db_path, root, no_backup=True)

            self.assertEqual(self.labels(db_path), [])
            self.assertEqual(self.history_paths(db_path), [])

    def test_verified_batch_reuses_recovery_backup_for_rolled_back_preflight(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "migrations"
            backup_dir = Path(tmpdir) / "backups"
            db_path = Path(tmpdir) / "anime.sqlite"
            self.create_db(db_path)
            self.write_migration(
                root,
                "2026-07-09_verified",
                "00_verified.sql",
                "insert into migration_log(label) values ('verified');",
            )

            copy_database = db_migrate.copy_database
            quick_check = db_migrate.verify_quick_integrity
            full_check = db_migrate.verify_connection
            preflight = db_migrate.preflight_migrations
            with patch("scripts.db_migrate.copy_database", wraps=copy_database) as copy:
                with patch(
                    "scripts.db_migrate.verify_quick_integrity",
                    wraps=quick_check,
                ) as quick:
                    with patch(
                        "scripts.db_migrate.verify_connection",
                        wraps=full_check,
                    ) as full:
                        with patch(
                            "scripts.db_migrate.preflight_migrations",
                            wraps=preflight,
                        ) as prove:
                            result = db_migrate.apply_pending(
                                db_path,
                                root,
                                backup_dir=backup_dir,
                                verify=True,
                            )

            self.assertEqual(copy.call_count, 1)
            self.assertEqual(quick.call_count, 1)
            self.assertEqual(full.call_count, 2)
            self.assertEqual(Path(prove.call_args.args[0]), result["backup"])
            self.assertEqual(self.labels(db_path), ["verified"])

            backup = sqlite3.connect(result["backup"])
            try:
                self.assertEqual(
                    backup.execute("select label from migration_log order by id").fetchall(),
                    [],
                )
                self.assertFalse(db_migrate.history_table_exists(backup))
                self.assertEqual(backup.execute("pragma integrity_check").fetchone()[0], "ok")
            finally:
                backup.close()

    def test_preflight_leaves_original_untouched_when_final_fk_check_would_fail(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "migrations"
            db_path = Path(tmpdir) / "anime.sqlite"
            con = sqlite3.connect(db_path)
            con.executescript(
                """
                create table parent(id integer primary key);
                create table child(parent_id integer references parent(id));
                create table migration_log (id integer primary key, label text not null);
                insert into child(parent_id) values (999);
                """
            )
            con.commit()
            con.close()
            self.write_migration(
                root,
                "2026-07-08_marker",
                "00_marker.sql",
                "insert into migration_log(label) values ('must-not-commit');",
            )

            with self.assertRaises(db_migrate.MigrationError):
                db_migrate.apply_pending(db_path, root, no_backup=True, verify=True)

            self.assertEqual(self.labels(db_path), [])
            con = sqlite3.connect(db_path)
            try:
                self.assertFalse(db_migrate.history_table_exists(con))
            finally:
                con.close()

    def test_statement_parser_checks_completeness_only_at_statement_boundaries(self):
        sql = "-- " + ("x" * 200_000) + "\nselect 1;\nselect 2;"
        original = sqlite3.complete_statement
        with patch(
            "scripts.db_migrate.sqlite3.complete_statement",
            wraps=original,
        ) as complete:
            statements = list(db_migrate.iter_sql_statements(sql))

        self.assertEqual(len(statements), 2)
        self.assertEqual(complete.call_count, 2)

    def test_integrity_repair_migration_removes_watch_orphans_and_adds_index(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "migrations"
            db_path = Path(tmpdir) / "anime.sqlite"
            con = sqlite3.connect(db_path)
            con.executescript(
                """
                create table users(id integer primary key);
                create table anime(id integer primary key);
                create table episodes(id integer primary key, anime_id integer references anime(id));
                create table video_sources(
                    id integer primary key,
                    episode_id integer references episodes(id),
                    provider_id text,
                    embed_url text
                );
                create table user_title_state(
                    user_id integer references users(id),
                    anime_id integer references anime(id),
                    primary key(user_id, anime_id)
                );
                create table user_watch_events(
                    id integer primary key,
                    user_id integer references users(id),
                    anime_id integer references anime(id),
                    episode_id integer references episodes(id),
                    video_source_id integer references video_sources(id),
                    source_anime_id integer references anime(id)
                );
                create table user_episode_state(
                    user_id integer references users(id),
                    anime_id integer references anime(id),
                    episode_id integer references episodes(id),
                    video_source_id integer references video_sources(id),
                    source_anime_id integer references anime(id),
                    primary key(user_id, anime_id, episode_id)
                );
                insert into user_title_state values (99, 98);
                insert into user_watch_events values (1, 99, 98, 97, 96, 95);
                insert into user_episode_state values (99, 98, 97, 96, 95);
                """
            )
            con.commit()
            con.close()
            repair_sql = (
                Path(__file__).parent
                / "migrations/2026-07-09_zz-data-integrity-repair/00_repair_foreign_keys_and_provider_index.sql"
            ).read_text(encoding="utf-8")
            self.write_migration(root, "2026-07-09_zz-data-integrity-repair", "00_repair.sql", repair_sql)

            db_migrate.apply_pending(db_path, root, no_backup=True, verify=True)

            con = sqlite3.connect(db_path)
            try:
                self.assertEqual(con.execute("pragma foreign_key_check").fetchall(), [])
                self.assertEqual(con.execute("select count(*) from user_watch_events").fetchone()[0], 0)
                self.assertIsNotNone(
                    con.execute(
                        "select 1 from sqlite_master where type='index' and name='idx_video_sources_provider_playable'"
                    ).fetchone()
                )
            finally:
                con.close()

    def test_external_rating_index_is_partial_covering_and_used_by_hot_query(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "migrations"
            db_path = Path(tmpdir) / "anime.sqlite"
            con = scrape_animego.init_db(db_path)
            try:
                for anime_id in range(1, 21):
                    con.execute(
                        """
                        insert into anime(id, slug, title, url, scraped_at)
                        values (?, ?, ?, ?, '2026-07-09T00:00:00+00:00')
                        """,
                        (
                            anime_id,
                            f"rating-{anime_id}",
                            f"Rating {anime_id}",
                            f"https://example.test/rating-{anime_id}",
                        ),
                    )
                    con.execute(
                        "insert into anime_fields(anime_id, label, value) values (?, 'Статус', 'Онгоинг')",
                        (anime_id,),
                    )
                con.execute(
                    "insert into anime_fields(anime_id, label, value) values (1, 'IMDB', '8.4')"
                )
                con.execute(
                    "insert into anime_fields(anime_id, label, value) values (2, 'Shikimori', '7.9')"
                )
                con.commit()
            finally:
                con.close()

            migration_path = (
                Path(__file__).parent
                / "migrations/2026-07-09_zzzz-external-rating-index/00_add_external_rating_index.sql"
            )
            migration_sql = migration_path.read_text(encoding="utf-8")
            self.write_migration(
                root,
                migration_path.parent.name,
                migration_path.name,
                migration_sql,
            )
            db_migrate.apply_pending(db_path, root, no_backup=True, verify=True)

            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row
            try:
                plan = con.execute(
                    f"""
                    explain query plan
                    select anime_id, label, value
                    from anime_fields
                    where {server.EXTERNAL_RATING_FIELD_PREDICATE_SQL}
                    """
                ).fetchall()
                plan_text = " ".join(str(row[3]) for row in plan)
                self.assertIn(
                    f"USING COVERING INDEX {server.EXTERNAL_RATING_INDEX_NAME}",
                    plan_text,
                )
                self.assertEqual(
                    server.load_external_ratings(con),
                    {
                        1: {
                            "external_score": 8.4,
                            "external_score_source": "IMDB",
                            "priority": 4,
                        },
                        2: {
                            "external_score": 7.9,
                            "external_score_source": "Shikimori",
                            "priority": 3,
                        },
                    },
                )
                index_sql = con.execute(
                    "select sql from sqlite_master where type='index' and name=?",
                    (server.EXTERNAL_RATING_INDEX_NAME,),
                ).fetchone()[0]
                normalized_index_sql = " ".join(index_sql.lower().split())
                normalized_predicate = " ".join(
                    server.EXTERNAL_RATING_FIELD_PREDICATE_SQL.lower().split()
                )
                self.assertIn(normalized_predicate, normalized_index_sql)
            finally:
                con.close()

            runtime_db = Path(tmpdir) / "runtime.sqlite"
            runtime_con = scrape_animego.init_db(runtime_db)
            try:
                self.assertTrue(server.ensure_runtime_indexes(runtime_con))
                self.assertIsNotNone(
                    runtime_con.execute(
                        "select 1 from sqlite_master where type='index' and name=?",
                        (server.EXTERNAL_RATING_INDEX_NAME,),
                    ).fetchone()
                )
            finally:
                runtime_con.close()

            self.assertGreater(
                migration_path.parent.name,
                "2026-07-09_zzz-catalog-cache-revision",
            )

    def test_transaction_control_inside_script_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "migrations"
            db_path = Path(tmpdir) / "anime.sqlite"
            self.create_db(db_path)
            self.write_migration(root, "2026-07-08_bad", "00_bad.sql", "begin; insert into migration_log(label) values ('bad'); commit;")

            with self.assertRaises(db_migrate.MigrationError):
                db_migrate.apply_pending(db_path, root, no_backup=True)

            self.assertEqual(self.labels(db_path), [])

    def test_prepare_database_can_auto_apply_migrations_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "migrations"
            db_path = Path(tmpdir) / "anime.sqlite"
            self.write_migration(
                root,
                "2026-07-08_auto",
                "00_create-auto-table.sql",
                "create table auto_migrated (value text not null);",
            )

            with patch.dict(
                "os.environ",
                {
                    "ANIME_AUTO_MIGRATE": "1",
                    "ANIME_MIGRATIONS_ROOT": str(root),
                    "ANIME_MIGRATION_NO_BACKUP": "1",
                },
            ):
                server.prepare_database(db_path)

            con = sqlite3.connect(db_path)
            try:
                self.assertIsNotNone(
                    con.execute(
                        "select 1 from sqlite_master where type = 'table' and name = 'auto_migrated'"
                    ).fetchone()
                )
                self.assertEqual(
                    con.execute(f"select count(*) from {db_migrate.HISTORY_TABLE}").fetchone()[0],
                    1,
                )
            finally:
                con.close()

    def test_data_diff_generates_catalog_upserts_and_applies_cleanly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            before_db = Path(tmpdir) / "before.sqlite"
            after_db = Path(tmpdir) / "after.sqlite"
            migration_root = Path(tmpdir) / "migrations"
            applied_db = Path(tmpdir) / "applied.sqlite"

            before_con = scrape_animego.init_db(before_db)
            before_con.close()
            sync_videos.copy_database(before_db, after_db)
            sync_videos.copy_database(before_db, applied_db)

            con = scrape_animego.init_db(after_db)
            try:
                scraped_at = server.now_iso()
                item = {
                    "id": 9001,
                    "slug": "generated-title",
                    "title": "Generated Title",
                    "subtitle": None,
                    "url": "https://example.test/generated-title",
                    "cover_url": None,
                    "source": "test",
                    "source_id": "generated-title",
                    "listing_score": None,
                    "kind": "TV",
                    "year": "2026",
                    "genres": ["Action"],
                    "listing_description": None,
                }
                detail = {
                    "title": "Generated Title",
                    "cover_url": None,
                    "aggregate_score": None,
                    "aggregate_count": None,
                    "date_published": None,
                    "content_rating": None,
                    "fields": {"Тип": "TV"},
                    "genres": ["Action"],
                    "dubbings": ["Test Voice"],
                    "description": "Generated by test",
                    "schema_data": {},
                }
                episode = {
                    "id": 9001001,
                    "number": "1",
                    "title": "Episode 1",
                    "release_label": None,
                    "episode_type": "episode",
                    "description": None,
                }
                provider = {
                    "provider_id": "test-provider-1",
                    "provider_title": "Test Provider",
                    "translation_id": 990001,
                    "translation_title": "Test Voice",
                    "embed_host": "example.test",
                    "embed_url": "https://example.test/embed/1",
                    "embed_url_redacted": "https://example.test/embed/<redacted>",
                }
                scrape_animego.upsert_anime(
                    con, item, detail, scraped_at, authoritative_metadata=True
                )
                scrape_animego.upsert_episode(con, item["id"], episode, True, None, scraped_at)
                scrape_animego.upsert_provider(con, item["id"], episode["id"], provider, True, scraped_at)
                con.commit()
            finally:
                con.close()

            sql = db_data_diff.generate_data_migration_sql(before_db, after_db)
            self.assertIn('insert into "anime"', sql)
            self.assertIn('insert into "video_sources"', sql)

            migration_path = sync_videos.write_migration_file(
                migration_root,
                "2026-07-08_generated",
                "00_generated.sql",
                sql,
            )
            self.assertTrue(migration_path.exists())
            db_migrate.apply_pending(applied_db, migration_root, no_backup=True)

            before_rows = sqlite3.connect(after_db)
            after_rows = sqlite3.connect(applied_db)
            try:
                for table in db_data_diff.CATALOG_TABLES:
                    columns = db_data_diff.table_columns(before_rows, table)
                    if table == "video_sources":
                        columns = [column for column in columns if column != "id"]
                    select_columns = ", ".join(db_data_diff.quote_identifier(column) for column in columns)
                    order_columns = ", ".join(db_data_diff.quote_identifier(column) for column in columns)
                    self.assertEqual(
                        before_rows.execute(
                            f"select {select_columns} from {table} order by {order_columns}"
                        ).fetchall(),
                        after_rows.execute(
                            f"select {select_columns} from {table} order by {order_columns}"
                        ).fetchall(),
                        table,
                    )
            finally:
                before_rows.close()
                after_rows.close()

    def test_emit_update_migration_skips_audit_only_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "anime.sqlite"
            migration_root = Path(tmpdir) / "migrations"
            con = scrape_animego.init_db(db_path)
            sync_videos.ensure_sync_tables(con)
            con.commit()
            con.close()

            args = sync_videos.parse_args(
                [
                    "--db",
                    str(db_path),
                    "--mode",
                    "manual",
                    "--yummy-ref",
                    "dummy-title",
                    "--emit-migration",
                    "audit-only",
                    "--migrations-root",
                    str(migration_root),
                ]
            )

            def write_audit_only(sync_args):
                con = sqlite3.connect(sync_args.db)
                try:
                    sync_videos.ensure_sync_tables(con)
                    sync_videos.write_sync_run(
                        con,
                        "manual",
                        "yummyanime",
                        server.now_iso(),
                        {"known_skipped": 1},
                    )
                    con.commit()
                finally:
                    con.close()
                return {"yummyanime": {"known_skipped": 1}}

            with patch("sync_videos.run_sync", side_effect=write_audit_only):
                self.assertIsNone(sync_videos.emit_update_migration(args))

            self.assertFalse(migration_root.exists())

    def test_emit_update_migration_releases_live_lock_after_single_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "anime.sqlite"
            lock_path = Path(tmpdir) / "anime.operation.lock"
            migration_root = Path(tmpdir) / "migrations"
            scrape_animego.init_db(db_path).close()
            args = sync_videos.parse_args(
                [
                    "--db",
                    str(db_path),
                    "--mode",
                    "manual",
                    "--yummy-ref",
                    "dummy-title",
                    "--emit-migration",
                    "snapshot-scope",
                    "--migrations-root",
                    str(migration_root),
                    "--lock-file",
                    str(lock_path),
                ]
            )

            def assert_live_lock_released(_sync_args):
                with sync_videos.FileLock(lock_path, wait=False):
                    pass
                return {"yummyanime": {"known_skipped": 1}}

            copy_database = sync_videos.copy_database
            with patch("sync_videos.copy_database", wraps=copy_database) as copy:
                with patch("sync_videos.run_sync", side_effect=assert_live_lock_released):
                    self.assertIsNone(sync_videos.emit_update_migration(args))

            self.assertEqual(copy.call_count, 2)
            first_source, first_target = map(Path, copy.call_args_list[0].args)
            second_source, _second_target = map(Path, copy.call_args_list[1].args)
            self.assertEqual(first_source, db_path)
            self.assertEqual(second_source, first_target)
            self.assertFalse(migration_root.exists())

    def test_write_migration_files_splits_large_sql_on_statement_boundaries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            migration_root = Path(tmpdir) / "migrations"
            paths = sync_videos.write_migration_files(
                migration_root,
                "2026-07-08_split",
                "00_data-update.sql",
                "-- generated\n",
                [
                    "create table split_test(id integer primary key);",
                    "insert into split_test(id) values (1);",
                    "insert into split_test(id) values (2);",
                ],
                max_bytes=90,
            )

            self.assertGreater(len(paths), 1)
            self.assertEqual(
                [path.name for path in paths],
                ["00_data-update-00.sql", "00_data-update-01.sql"],
            )

            db_path = Path(tmpdir) / "anime.sqlite"
            scrape_animego.init_db(db_path).close()
            db_migrate.apply_pending(db_path, migration_root, no_backup=True)
            con = sqlite3.connect(db_path)
            try:
                self.assertEqual(con.execute("select count(*) from split_test").fetchone()[0], 2)
            finally:
                con.close()

    def test_write_migration_files_consumes_one_shot_generator_in_bounded_chunks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            migration_root = Path(tmpdir) / "migrations"

            def statements():
                yield "create table streamed(id integer primary key);"
                for index in range(1_000):
                    yield f"insert into streamed(id) values ({index});"

            paths = sync_videos.write_migration_files(
                migration_root,
                "2026-07-09_streamed",
                "00_data.sql",
                "-- generated\n",
                statements(),
                max_bytes=2_048,
            )

            self.assertGreater(len(paths), 1)
            self.assertTrue(all(path.stat().st_size <= 2_100 for path in paths))
            db_path = Path(tmpdir) / "anime.sqlite"
            self.create_db(db_path)
            db_migrate.apply_pending(db_path, migration_root, no_backup=True)
            con = sqlite3.connect(db_path)
            try:
                self.assertEqual(con.execute("select count(*) from streamed").fetchone()[0], 1_000)
            finally:
                con.close()


if __name__ == "__main__":
    unittest.main()
