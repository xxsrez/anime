import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts import db_migrate


ROOT = Path(__file__).resolve().parent
MIGRATION = ROOT / "migrations/2026-07-09_zzzzz-user-library-state/00_add_user_library_state.sql"


class UserLibraryMigrationTest(unittest.TestCase):
    def test_migration_backfills_legacy_state_and_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "anime.sqlite"
            root = Path(tmpdir) / "migrations"
            migration_dir = root / MIGRATION.parent.name
            migration_dir.mkdir(parents=True)
            shutil.copy2(MIGRATION, migration_dir / MIGRATION.name)

            con = sqlite3.connect(db_path)
            con.executescript(
                """
                create table user_title_state (
                    user_id integer not null,
                    anime_id integer not null,
                    is_favorite integer not null default 0,
                    progress_episode_number integer,
                    watched integer not null default 0,
                    updated_at text not null,
                    primary key(user_id, anime_id)
                );
                insert into user_title_state values (1, 10, 1, null, 0, 'favorite-at');
                insert into user_title_state values (1, 11, 0, 3, 0, 'watching-at');
                insert into user_title_state values (1, 12, 0, 12, 1, 'completed-at');
                """
            )
            con.commit()
            con.close()

            result = db_migrate.apply_pending(db_path, root, no_backup=True)
            self.assertEqual(len(result["applied"]), 1)

            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row
            rows = {
                row["anime_id"]: dict(row)
                for row in con.execute("select * from user_title_state order by anime_id")
            }
            indexes = {
                row[1]
                for row in con.execute("pragma index_list(user_title_state)")
            }
            history = con.execute("select count(*) from schema_migrations").fetchone()[0]
            con.close()

            self.assertEqual(rows[10]["watch_status"], None)
            self.assertEqual(rows[10]["favorite_updated_at"], "favorite-at")
            self.assertEqual(rows[11]["watch_status"], "watching")
            self.assertEqual(rows[11]["watch_status_updated_at"], "watching-at")
            self.assertEqual(rows[12]["watch_status"], "completed")
            self.assertEqual(rows[12]["not_interested"], 0)
            self.assertIn("idx_user_title_state_user_watch_status", indexes)
            self.assertIn("idx_user_title_state_user_not_interested", indexes)
            self.assertEqual(history, 1)


if __name__ == "__main__":
    unittest.main()
