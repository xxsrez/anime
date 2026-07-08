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
            return [
                row[0]
                for row in con.execute(
                    f"select path from {db_migrate.HISTORY_TABLE} order by path"
                )
            ]
        finally:
            con.close()

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
                self.assertEqual(
                    con.execute(f"select count(*) from {db_migrate.HISTORY_TABLE}").fetchone()[0],
                    0,
                )
            finally:
                con.close()

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
                scrape_animego.upsert_anime(con, item, detail, scraped_at)
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


if __name__ == "__main__":
    unittest.main()
