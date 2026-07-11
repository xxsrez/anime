import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts import db_migrate


ROOT = Path(__file__).resolve().parent
MIGRATION = (
    ROOT
    / "migrations/2026-07-11_title-library-status/00_normalize_title_library_status.sql"
)


class TitleLibraryStatusMigrationTest(unittest.TestCase):
    def test_migration_normalizes_legacy_rows_without_losing_history(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "anime.sqlite"
            migrations_root = Path(tmpdir) / "migrations"
            target = migrations_root / MIGRATION.parent.name / MIGRATION.name
            target.parent.mkdir(parents=True)
            shutil.copy2(MIGRATION, target)

            con = sqlite3.connect(db_path)
            con.executescript(
                """
                create table user_title_state (
                    user_id integer not null,
                    anime_id integer not null,
                    is_favorite integer not null default 0,
                    progress_episode_number integer,
                    watched integer not null default 0,
                    watch_status text,
                    not_interested integer not null default 0,
                    updated_at text not null,
                    favorite_updated_at text,
                    watch_status_updated_at text,
                    not_interested_updated_at text,
                    primary key(user_id, anime_id)
                );
                create table user_episode_state (
                    user_id integer not null,
                    anime_id integer not null,
                    episode_id integer not null,
                    started_at text,
                    last_event_type text,
                    last_confidence real,
                    updated_at text not null,
                    primary key(user_id, anime_id, episode_id)
                );

                insert into user_title_state values
                    (1, 10, 0, 10, 1, null,        0, 'u10', null, 's10', null),
                    (1, 11, 0, 11, 0, 'completed', 0, 'u11', null, 's11', null),
                    (1, 12, 0, 12, 0, 'watching',  0, 'u12', null, 's12', null),
                    (1, 13, 0, 13, 0, 'paused',    0, 'u13', null, 's13', null),
                    (1, 14, 0, 14, 0, null,        0, 'u14', null, 's14', null),
                    (1, 15, 0, null, 0, null,      0, 'u15', null, null,  null),
                    (1, 16, 0, 16, 0, 'planned',   0, 'u16', null, 's16', null),
                    (1, 17, 0, 17, 0, 'dropped',   0, 'u17', null, 's17', null),
                    (1, 18, 0, 18, 0, 'unknown',   0, 'u18', null, 's18', null),
                    (1, 19, 0, 19, 0, 'none',      0, 'u19', null, 's19', null),
                    (1, 20, 1, null, 0, 'none',    1, 'u20', 'f20', null, 'n20');

                insert into user_episode_state values
                    (1, 12, 120, 'started-12', 'heartbeat', 0.8, 'episode-12'),
                    (1, 16, 160, 'started-16', 'heartbeat', 0.8, 'episode-16');
                """
            )
            con.commit()
            con.close()

            first = db_migrate.apply_pending(
                db_path,
                migrations_root,
                no_backup=True,
            )
            second = db_migrate.apply_pending(
                db_path,
                migrations_root,
                no_backup=True,
            )

            self.assertEqual(
                [migration.path for migration, _ in first["applied"]],
                [f"{MIGRATION.parent.name}/{MIGRATION.name}"],
            )
            self.assertEqual(second["applied"], [])

            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row
            try:
                rows = {
                    row["anime_id"]: dict(row)
                    for row in con.execute(
                        "select * from user_title_state order by anime_id"
                    )
                }
                history_count = con.execute(
                    f"select count(*) from {db_migrate.HISTORY_TABLE}"
                ).fetchone()[0]
                episode_rows = {
                    row["anime_id"]: dict(row)
                    for row in con.execute(
                        "select * from user_episode_state order by anime_id"
                    )
                }
            finally:
                con.close()

            expected = {
                10: ("completed", 1, 10),
                11: ("completed", 1, 11),
                12: ("watching", 0, 12),
                13: ("watching", 0, 13),
                14: ("watching", 0, 14),
                15: ("none", 0, None),
                16: ("none", 0, None),
                17: ("none", 0, None),
                18: ("none", 0, None),
                19: ("none", 0, None),
                20: ("none", 0, None),
            }
            for anime_id, values in expected.items():
                with self.subTest(anime_id=anime_id):
                    self.assertEqual(
                        (
                            rows[anime_id]["watch_status"],
                            rows[anime_id]["watched"],
                            rows[anime_id]["progress_episode_number"],
                        ),
                        values,
                    )
                    self.assertEqual(rows[anime_id]["updated_at"], f"u{anime_id}")

            self.assertEqual(rows[13]["watch_status_updated_at"], "s13")
            self.assertEqual(rows[16]["watch_status_updated_at"], "s16")
            self.assertEqual(rows[20]["favorite_updated_at"], "f20")
            self.assertEqual(rows[20]["not_interested_updated_at"], "n20")
            self.assertEqual(rows[20]["not_interested"], 0)
            self.assertEqual(episode_rows[12]["started_at"], "started-12")
            self.assertIsNone(episode_rows[16]["started_at"])
            self.assertEqual(episode_rows[16]["last_event_type"], "manual_clear")
            self.assertEqual(episode_rows[16]["last_confidence"], 1.0)
            self.assertEqual(episode_rows[16]["updated_at"], "s16")
            self.assertEqual(history_count, 1)


if __name__ == "__main__":
    unittest.main()
