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
            for anime_id, source, slug, title in (
                (101, "animego", "shared-animego", "Shared Update"),
                (102, "yummyanime", "shared-yummy", "Shared Update"),
                (103, "animego", "second-update", "Second Update"),
                (104, "animego", "priority-update", "Priority Update"),
            ):
                con.execute(
                    """
                    insert into anime(id, slug, title, url, source, source_id, year, scraped_at)
                    values (?, ?, ?, ?, ?, ?, '2026', ?)
                    """,
                    (
                        anime_id,
                        slug,
                        title,
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
            for index, (anime_id, title) in enumerate(((103, "Second Update"), (104, "Priority Update")), start=20):
                content_updates.insert_event(
                    con,
                    None,
                    "new_episode",
                    anime_id,
                    source="animego",
                    source_id=str(anime_id),
                    episode_number="1",
                    title=title,
                    description=f"{title} episode",
                    occurred_at=(recent - dt.timedelta(minutes=index)).isoformat(timespec="seconds"),
                    dedupe_key=f"test:title:{anime_id}",
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

    def test_filter_totals_and_title_pages_are_independent(self):
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
            limit=2,
            event_type="all",
            offset=0,
        )
        self.assertEqual(first["summary"]["event_count"], 12)
        self.assertEqual(first["summary"]["event_counts"]["new_episode"], 10)
        self.assertEqual(first["summary"]["event_counts"]["new_translation"], 2)
        self.assertEqual(first["summary"]["updated_title_count"], 3)
        self.assertEqual(first["pagination"]["returned"], 2)
        self.assertTrue(first["pagination"]["has_more"])
        self.assertEqual(first["pagination"]["next_offset"], 2)
        shared = next(item for item in first["items"] if item["title"] == "Shared Update")
        self.assertEqual(shared["report"]["event_count"], 10)
        self.assertEqual(shared["report"]["episode_numbers"], [str(value) for value in range(1, 9)])

        second = server.get_content_updates(
            self.db_path,
            days=7,
            limit=2,
            event_type="all",
            offset=first["pagination"]["next_offset"],
        )
        self.assertEqual(second["summary"], first["summary"])
        self.assertEqual(second["pagination"]["returned"], 1)
        self.assertFalse(second["pagination"]["has_more"])
        self.assertFalse({item["id"] for item in first["items"]} & {item["id"] for item in second["items"]})

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
        self.assertFalse(rare["pagination"]["has_more"])
        self.assertEqual(rare["items"][0]["report"]["event_count"], 2)
        self.assertEqual(len(rare["items"][0]["report"]["translations"]), 2)

    def test_favorite_and_watching_titles_are_ranked_first_without_cache_leak(self):
        con = server.connect(self.db_path)
        try:
            user = server.upsert_google_user(
                con,
                {
                    "sub": "content-priority",
                    "email": "priority@example.test",
                    "email_verified": True,
                    "name": "Priority",
                    "picture": None,
                },
            )
            con.commit()
        finally:
            con.close()
        server.update_user_state(104, {"is_favorite": True}, self.db_path, user["id"])
        server.update_user_state(103, {"watch_status": "watching"}, self.db_path, user["id"])

        personalized = server.get_content_updates(self.db_path, days=7, limit=3, user_id=user["id"], offset=0)
        anonymous = server.get_content_updates(self.db_path, days=7, limit=3, offset=0)

        self.assertEqual([item["id"] for item in personalized["items"][:2]], [103, 104])
        self.assertTrue(all(item["is_priority"] for item in personalized["items"][:2]))
        self.assertEqual(anonymous["items"][0]["id"], 101)
        self.assertFalse(any(item["is_priority"] for item in anonymous["items"]))

    def test_priority_requires_a_new_episode_beyond_user_progress(self):
        con = server.connect(self.db_path)
        now = dt.datetime.now(dt.timezone.utc)
        try:
            for anime_id, slug, title in (
                (105, "caught-up-favorite", "Caught Up Favorite"),
                (106, "watching-unseen", "Watching Unseen"),
                (107, "completed-title", "Completed Title"),
                (108, "favorite-unseen", "Favorite Unseen"),
                (109, "translation-only", "Translation Only"),
            ):
                con.execute(
                    """
                    insert into anime(id, slug, title, url, source, source_id, year, scraped_at)
                    values (?, ?, ?, ?, 'animego', ?, '2026', ?)
                    """,
                    (
                        anime_id,
                        slug,
                        title,
                        f"https://example.test/{slug}",
                        str(anime_id),
                        server.now_iso(),
                    ),
                )
            for anime_id, episode_number, minutes in (
                (105, "2", 2),
                (106, "3", 5),
                (107, "4", 4),
                (108, "1", 1),
            ):
                content_updates.insert_event(
                    con,
                    None,
                    "new_episode",
                    anime_id,
                    source="animego",
                    source_id=str(anime_id),
                    episode_number=episode_number,
                    title=f"Update {anime_id}",
                    occurred_at=(now + dt.timedelta(minutes=minutes)).isoformat(timespec="seconds"),
                    dedupe_key=f"priority-unseen:{anime_id}",
                )
            content_updates.insert_event(
                con,
                None,
                "new_translation",
                109,
                source="animego",
                source_id="109",
                episode_number="3",
                translation_title="Dream Cast",
                title="Translation Only",
                occurred_at=(now + dt.timedelta(minutes=6)).isoformat(timespec="seconds"),
                dedupe_key="priority-unseen:109",
            )
            user = server.upsert_google_user(
                con,
                {
                    "sub": "unseen-priority",
                    "email": "unseen-priority@example.test",
                    "email_verified": True,
                    "name": "Unseen Priority",
                    "picture": None,
                },
            )
            con.commit()
        finally:
            con.close()
        server.invalidate_catalog_cache(self.db_path)

        server.update_user_state(
            105,
            {"is_favorite": True, "progress_episode_number": 2},
            self.db_path,
            user["id"],
        )
        server.update_user_state(106, {"progress_episode_number": 2}, self.db_path, user["id"])
        server.update_user_state(
            107,
            {"is_favorite": True, "progress_episode_number": 2},
            self.db_path,
            user["id"],
        )
        server.update_user_state(107, {"watch_status": "completed"}, self.db_path, user["id"])
        server.update_user_state(108, {"is_favorite": True}, self.db_path, user["id"])
        server.update_user_state(109, {"is_favorite": True}, self.db_path, user["id"])

        personalized = server.get_content_updates(
            self.db_path,
            days=7,
            limit=20,
            user_id=user["id"],
            offset=0,
        )
        items = {item["id"]: item for item in personalized["items"]}

        self.assertEqual([item["id"] for item in personalized["items"][:2]], [106, 108])
        self.assertTrue(items[106]["has_unseen_episode"])
        self.assertTrue(items[108]["has_unseen_episode"])
        self.assertFalse(items[105]["has_unseen_episode"])
        self.assertFalse(items[107]["has_unseen_episode"])
        self.assertFalse(items[109]["has_unseen_episode"])
        self.assertFalse(items[105]["is_priority"])
        self.assertFalse(items[107]["is_priority"])
        self.assertFalse(items[109]["is_priority"])

        server.update_user_state(106, {"progress_episode_number": 3}, self.db_path, user["id"])
        caught_up = server.get_content_updates(
            self.db_path,
            days=7,
            limit=20,
            user_id=user["id"],
            offset=0,
        )
        caught_up_items = {item["id"]: item for item in caught_up["items"]}
        self.assertFalse(caught_up_items[106]["has_unseen_episode"])
        self.assertFalse(caught_up_items[106]["is_priority"])
        self.assertEqual([item["id"] for item in caught_up["items"] if item["is_priority"]], [108])

    def test_report_keeps_all_change_types_and_groups_translation_episodes(self):
        now = server.now_iso()
        events = [
            {"event_type": "new_title", "occurred_at": now, "metadata": {"episode_count": 12}},
            {
                "event_type": "new_episode",
                "occurred_at": now,
                "episode_number": "13",
                "metadata": {"provider_count": 3},
            },
            *[
                {
                    "event_type": "new_translation",
                    "occurred_at": now,
                    "episode_number": str(number),
                    "translation_title": "Dream Cast",
                    "metadata": {},
                }
                for number in range(1, 21)
            ],
            {
                "event_type": "new_provider",
                "occurred_at": now,
                "episode_number": "13",
                "provider_title": "Kodik",
                "translation_title": "Dream Cast",
                "metadata": {},
            },
            {
                "event_type": "new_provider",
                "occurred_at": now,
                "episode_number": "13",
                "provider_title": "Kodik",
                "translation_title": "AniDub",
                "metadata": {},
            },
        ]

        report = server.content_update_report(events)

        self.assertEqual(report["new_title"]["episode_count"], 12)
        self.assertEqual(report["episode_numbers"], ["13"])
        self.assertEqual(report["new_episode_provider_count"], 3)
        self.assertEqual(len(report["translations"]), 1)
        self.assertEqual(report["translations"][0]["episode_count"], 20)
        self.assertEqual(report["translations"][0]["event_count"], 20)
        self.assertEqual(len(report["providers"]), 2)
        self.assertEqual(
            {entry["translation_title"] for entry in report["providers"]},
            {"Dream Cast", "AniDub"},
        )

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
