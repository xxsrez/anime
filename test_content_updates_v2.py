#!/usr/bin/env python3
import datetime as dt
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

import content_updates
import scrape_animego
import server


class ContentUpdatesV2Test(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "anime.sqlite"
        con = scrape_animego.init_db(self.db_path)
        content_updates.ensure_schema(con)
        now = server.now_iso()
        try:
            for anime_id, source, slug in (
                (101, "animego", "shared-animego"),
                (102, "yummyanime", "shared-yummy"),
            ):
                con.execute(
                    """
                    insert into anime(id, slug, title, url, source, source_id, year, scraped_at)
                    values (?, ?, 'Shared Update', ?, ?, ?, '2026', ?)
                    """,
                    (
                        anime_id,
                        slug,
                        f"https://example.test/{slug}",
                        source,
                        str(anime_id),
                        now,
                    ),
                )

            recent = dt.datetime.now(dt.timezone.utc)
            for index in range(8):
                anime_id = 101 if index % 2 == 0 else 102
                content_updates.insert_event(
                    con,
                    None,
                    "new_episode",
                    anime_id,
                    source="animego",
                    source_id=str(anime_id),
                    episode_number=str(index + 1),
                    title="Shared Update",
                    description=f"Common {index}",
                    occurred_at=(recent - dt.timedelta(minutes=index)).isoformat(timespec="seconds"),
                    dedupe_key=f"test:common:{index}",
                )
            for index, anime_id in enumerate((101, 102)):
                content_updates.insert_event(
                    con,
                    None,
                    "new_translation",
                    anime_id,
                    source="animego",
                    source_id=str(anime_id),
                    episode_number="1",
                    translation_title=f"Rare {index}",
                    title="Shared Update",
                    description=f"Rare {index}",
                    occurred_at=(recent - dt.timedelta(days=2, minutes=index)).isoformat(timespec="seconds"),
                    dedupe_key=f"test:rare:{index}",
                )
            con.commit()
        finally:
            con.close()
        server.reset_database_initialization(self.db_path)
        server.invalidate_catalog_cache(self.db_path)

    def tearDown(self):
        server.invalidate_catalog_cache(self.db_path)
        server.reset_database_initialization(self.db_path)
        self.tmpdir.cleanup()

    def test_filter_totals_and_offset_pages_are_independent(self):
        con = server.connect(self.db_path)
        try:
            query_plan = con.execute(
                """
                explain query plan
                select id, event_type, occurred_at
                from content_update_events
                where occurred_at >= ? and event_type = ?
                order by occurred_at desc, id desc
                limit ? offset ?
                """,
                (content_updates.recent_cutoff(7), "new_translation", 2, 0),
            ).fetchall()
        finally:
            con.close()
        self.assertIn(
            "idx_content_update_events_type_at",
            " ".join(str(row["detail"]) for row in query_plan),
        )

        first = server.get_content_updates(
            self.db_path,
            days=7,
            limit=3,
            event_type="all",
            offset=0,
        )
        self.assertEqual(first["summary"]["event_count"], 10)
        self.assertEqual(first["summary"]["event_counts"]["new_episode"], 8)
        self.assertEqual(first["summary"]["event_counts"]["new_translation"], 2)
        self.assertEqual(first["summary"]["updated_title_count"], 1)
        self.assertEqual(first["pagination"]["returned"], 3)
        self.assertTrue(first["pagination"]["has_more"])
        self.assertEqual(first["pagination"]["next_offset"], 3)

        second = server.get_content_updates(
            self.db_path,
            days=7,
            limit=3,
            event_type="all",
            offset=first["pagination"]["next_offset"],
        )
        self.assertEqual(second["summary"], first["summary"])
        self.assertFalse(
            {event["id"] for event in first["events"]}
            & {event["id"] for event in second["events"]}
        )

        rare = server.get_content_updates(
            self.db_path,
            days=7,
            limit=1,
            event_type="new_translation",
            offset=0,
        )
        self.assertEqual(rare["summary"]["event_count"], 2)
        self.assertEqual(rare["summary"]["event_counts"]["new_translation"], 2)
        self.assertEqual([event["event_type"] for event in rare["events"]], ["new_translation"])
        self.assertEqual(rare["pagination"]["next_offset"], 1)

    def test_invalid_event_type_is_rejected_by_function_and_route(self):
        with self.assertRaisesRegex(ValueError, "invalid content update event_type"):
            server.get_content_updates(self.db_path, event_type="not-real")

        con = server.connect(self.db_path)
        try:
            user = server.upsert_google_user(
                con,
                {
                    "sub": "content-updates-v2",
                    "email": "updates-v2@example.test",
                    "email_verified": True,
                    "name": "Updates",
                    "picture": None,
                },
            )
            token, _ = server.create_session(con, user["id"])
            con.commit()
        finally:
            con.close()

        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.AnimeHandler)
        httpd.db_path = self.db_path
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
            connection.request(
                "GET",
                "/api/content-updates?event_type=not-real",
                headers={"Cookie": f"{server.SESSION_COOKIE_NAME}={token}"},
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
            self.assertEqual(response.status, 400)
            self.assertEqual(payload["error"], "invalid content update event_type")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
