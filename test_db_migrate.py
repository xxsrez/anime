#!/usr/bin/env python3
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
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


if __name__ == "__main__":
    unittest.main()
