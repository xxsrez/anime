#!/usr/bin/env python3
import http.client
import datetime as dt
from contextlib import closing
import io
import json
from pathlib import Path
import random
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import zipfile

import animego_scans
import scrape_animego
import server


class AnimeGoScansTest(unittest.TestCase):
    def test_retired_source_keeps_episodes_but_hides_link_and_prefers_live_variant(self):
        self.add_title(701, title="Retired source fixture", playable=True)
        self.add_title(20000701, title="Retired source fixture", source="yummyanime", playable=True)
        with closing(server.connect(self.db_path)) as con:
            con.execute("update anime set source_retired_at='2026-10-19T00:00:00Z' where id=701")
            con.execute("update anime set url='https://ru.yummyani.me/catalog/item/fixture',source_id='yummyani:701' where id=20000701")
            con.commit()
            task = animego_scans.task_payloads(con, [(701, "stale")])[0]
            self.assertTrue(task["source_retired"])
            self.assertIsNone(task["url"])
            import sync_videos
            import backfill_players
            self.assertNotIn(701, [r["id"] for r in sync_videos.animego_ongoing_rows(con)])
            self.assertNotIn(701, [r["id"] for r in backfill_players.playable_missing_rows(con)])
        server.invalidate_catalog_cache(self.db_path)
        detail = server.get_anime_detail(701, self.db_path)
        self.assertEqual(detail["id"], 20000701)
        self.assertEqual(set(detail["source_member_ids"]), {701, 20000701})
        archived = next(v for v in detail["source_variants"] if v["id"] == 701)
        self.assertIsNone(archived["url"])
        self.assertTrue(archived["source_retired_at"])
        self.assertTrue(any(s["source_anime_id"] == 701 for sources in detail["sources_by_episode"].values() for s in sources))

    def test_browser_recovery_handshake_does_not_fetch_or_mark_item_checked(self):
        self.add_title(3998)
        self.add_title(20026525, source="yummyanime")
        url = "https://ru.yummyani.me/catalog/item/fixture"
        with closing(server.connect(self.db_path)) as con:
            con.execute("update anime set url=? where id=20026525", (url,))
            con.commit()
        resolver = Mock(return_value={"id": 20026525, "source": "yummyanime", "url": url})
        job = self.create_job()
        body = {"anime_id": 3998, "error": "AnimeGO HTTP 404", "upstream_status": 404, "episodes": [], "browser_recovery": True}
        args = (self.db_path, job["job"]["id"], job["token"])
        with patch.object(animego_scans.yummy, "parse_modern_detail", side_effect=AssertionError("No server network")):
            result = animego_scans.submit_scan_result(*args, body, server.PLAYER_HOSTS, fallback_resolver=resolver)
            self.assertEqual(result["recovery_task"]["anime_id"], 20026525)
            self.assertEqual(result["recovery_task"]["url"], "https://api.yani.tv/anime/fixture?need_videos=true")
            with closing(server.connect(self.db_path)) as con:
                self.assertEqual(con.execute("select checked_items from animego_scan_jobs where id=?", (job["job"]["id"],)).fetchone()[0], 0)
            episode = {"id": 20026525002, "number": "2", "title": "2 серия"}
            provider = {**self.provider(), "episode_number": "2"}
            body["recovery_response"] = {"response": {"anime_id": 26525}}
            with patch.object(animego_scans.yummy, "parse_modern_payload", return_value=({"id": 20026525}, {}, [episode], [provider])) as parse:
                result = animego_scans.submit_scan_result(*args, body, server.PLAYER_HOSTS, fallback_resolver=resolver)
                parse.assert_called_once_with({"anime_id": 26525}, url)
            self.assertEqual(result["job"]["error_count"], 0)
            self.assertEqual(result["result"]["recovered_source"], "YummyAnime")
            replay = animego_scans.submit_scan_result(*args, body, server.PLAYER_HOSTS, fallback_resolver=resolver)
            self.assertEqual(replay["job"]["checked_items"], 1)

    def test_browser_recovery_rejects_substituted_source_and_reports_fetch_failure(self):
        self.add_title(3998)
        for raw in ({"response": {"anime_id": 1}}, {"response": {"anime_id": 26525, "episodes": {"count": 10**9}}}, None):
            with self.subTest(raw=raw):
                job = self.create_job()
                resolver = Mock(return_value={"id": 20026525, "source": "yummyanime", "url": "https://ru.yummyani.me/catalog/item/fixture"})
                body = {"anime_id": 3998, "error": "HTTP 404", "upstream_status": 404, "episodes": [], "browser_recovery": True}
                if raw is None:
                    body["recovery_error"] = True
                else:
                    body["recovery_response"] = raw
                with patch.object(animego_scans.yummy, "parse_modern_detail", side_effect=AssertionError("No server network")):
                    result = animego_scans.submit_scan_result(self.db_path, job["job"]["id"], job["token"], body, server.PLAYER_HOSTS, fallback_resolver=resolver)
                self.assertEqual(result["result"]["status"], "failed")
                self.assertEqual(result["job"]["error_count"], 1)
                animego_scans.complete_scan_job(self.db_path, job["job"]["id"], job["token"], {"stopped": True})

    def test_404_recovers_canonical_alternative_without_mixing_source_ids(self):
        self.add_title(3998)
        self.add_title(20026525, source="yummyanime")
        url = "https://ru.yummyani.me/catalog/item/fixture"
        with closing(server.connect(self.db_path)) as con:
            con.execute("update anime set url=? where id=20026525", (url,))
            con.commit()
        resolver = Mock(return_value={"id": 20026525, "source": "yummyanime", "url": url})
        job = self.create_job()
        payload = {"anime_id": 3998, "episodes": [], "error": "AnimeGO HTTP 404", "upstream_status": 404}
        episode = {"id": 20026525002, "number": "2", "title": "2 серия"}
        provider = {**self.provider(), "episode_number": "2"}
        with patch.object(animego_scans.yummy, "parse_modern_detail", return_value=({"id": 20026525}, {}, [episode], [provider])) as fetch:
            with self.assertRaises(animego_scans.ScanAuthenticationError):
                animego_scans.submit_scan_result(self.db_path, job["job"]["id"], "wrong", payload, server.PLAYER_HOSTS, fallback_resolver=resolver)
            fetch.assert_not_called()
            result = animego_scans.submit_scan_result(self.db_path, job["job"]["id"], job["token"], payload, server.PLAYER_HOSTS, fallback_resolver=resolver)
            replay = animego_scans.submit_scan_result(self.db_path, job["job"]["id"], job["token"], payload, server.PLAYER_HOSTS, fallback_resolver=resolver)
            self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result["result"]["recovered_source"], "YummyAnime")
        self.assertEqual(result["job"]["error_count"], 0)
        self.assertEqual(result["result"]["new_episode_count"], 1)
        self.assertEqual(replay["result"]["recovered_source"], "YummyAnime")
        self.assertEqual(replay["job"]["checked_items"], 1)
        with closing(server.connect(self.db_path)) as con:
            self.assertEqual(con.execute("select anime_id from episodes where id=20026525002").fetchone()[0], 20026525)
            self.assertEqual(con.execute("select count(*) from episodes where anime_id=3998").fetchone()[0], 0)
            original = con.execute("select last_error from animego_title_scan_state where anime_id=3998").fetchone()[0]
            self.assertIn("404", original)
            audit = con.execute("select error from animego_scan_job_items where job_id=?", (job["job"]["id"],)).fetchone()[0]
            self.assertIn("Recovered via YummyAnime #20026525", audit)

    def test_404_recovery_rejects_changed_identity_and_preserves_failure(self):
        self.add_title(3998)
        resolver = Mock(return_value={"id": 20026525, "source": "yummyanime", "url": "https://ru.yummyani.me/catalog/item/fixture"})
        job = self.create_job()
        payload = {"anime_id": 3998, "episodes": [], "error": "AnimeGO HTTP 404", "upstream_status": 404}
        with patch.object(animego_scans.yummy, "parse_modern_detail", return_value=({"id": 20000001}, {}, [], [])):
            result = animego_scans.submit_scan_result(self.db_path, job["job"]["id"], job["token"], payload, server.PLAYER_HOSTS, fallback_resolver=resolver)
        self.assertEqual(result["job"]["error_count"], 1)
        self.assertEqual(result["result"]["status"], "failed")
        self.assertIn("recovery failed", result["job"]["last_error"])

    def test_task_payload_includes_card_url_for_metadata_refresh(self):
        self.add_title(701)
        with closing(server.connect(self.db_path)) as con:
            tasks = animego_scans.task_payloads(con, [(701, "stale")])
        self.assertEqual(tasks[0]["url"], "https://animego.me/anime/scan-701-701")

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "anime.sqlite"
        con = scrape_animego.init_db(self.db_path)
        con.close()
        server.prepare_database(self.db_path)
        con = server.connect(self.db_path)
        try:
            self.user = server.upsert_google_user(
                con,
                {
                    "sub": "animego-scanner-user",
                    "email": "scanner@example.test",
                    "email_verified": True,
                    "name": "Scanner User",
                    "picture": None,
                },
            )
            con.commit()
        finally:
            con.close()

    def tearDown(self):
        server.invalidate_catalog_cache(self.db_path)
        server.reset_database_initialization(self.db_path)
        self.tmpdir.cleanup()

    def add_title(
        self,
        anime_id,
        *,
        title=None,
        source="animego",
        status="Онгоинг",
        episodes_text="12",
        playable=False,
        placeholder=False,
    ):
        con = server.connect(self.db_path)
        try:
            con.execute(
                """
                insert into anime (
                    id, slug, title, url, source, source_id, year, status,
                    episodes_text, scraped_at
                ) values (?, ?, ?, ?, ?, ?, '2026', ?, ?, ?)
                """,
                (
                    anime_id,
                    f"scan-{anime_id}",
                    title or f"Scan Title {anime_id}",
                    f"https://animego.me/anime/scan-{anime_id}-{anime_id}",
                    source,
                    str(anime_id),
                    status,
                    episodes_text,
                    animego_scans.iso_timestamp(),
                ),
            )
            if playable or placeholder:
                episode_id = anime_id * 100 + 1
                con.execute(
                    """
                    insert into episodes (
                        id, anime_id, number, title, has_video,
                        unavailable_reason, scraped_at
                    ) values (?, ?, '1', 'Original episode title', ?, ?, ?)
                    """,
                    (
                        episode_id,
                        anime_id,
                        1 if playable else 0,
                        None if playable else "not yet available",
                        animego_scans.iso_timestamp(),
                    ),
                )
                con.execute("insert or ignore into translations(id, title) values (1, 'Dream Cast')")
                con.execute(
                    """
                    insert into video_sources (
                        anime_id, episode_id, provider_id, provider_title,
                        translation_id, translation_title, embed_host,
                        embed_url, embed_url_redacted, scraped_at
                    ) values (?, ?, ?, 'Original provider', 1, 'Dream Cast', ?, ?, ?, ?)
                    """,
                    (
                        anime_id,
                        episode_id,
                        f"provider-{anime_id}",
                        "kodikplayer.com" if playable else None,
                        (
                            f"https://kodikplayer.com/serial/{anime_id}/existing-token/720p"
                            if playable
                            else None
                        ),
                        (
                            f"//kodikplayer.com/serial/{anime_id}/<redacted>/720p"
                            if playable
                            else None
                        ),
                        animego_scans.iso_timestamp(),
                    ),
                )
            con.commit()
        finally:
            con.close()
        server.invalidate_catalog_cache(self.db_path)

    def set_library_state(
        self,
        anime_id,
        *,
        favorite=False,
        watch_status="none",
        not_interested=False,
        updated_at="2026-07-11T10:00:00+00:00",
    ):
        con = server.connect(self.db_path)
        try:
            con.execute(
                """
                insert into user_title_state (
                    user_id, anime_id, is_favorite, watched, watch_status,
                    not_interested, updated_at
                ) values (?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    self.user["id"],
                    anime_id,
                    int(favorite),
                    watch_status,
                    int(not_interested),
                    updated_at,
                ),
            )
            con.commit()
        finally:
            con.close()

    def provider(self, provider_id="kodik-new", translation_id=2):
        embed_url = "https://kodikplayer.com/serial/500/new-secret-token/720p?episode=2"
        return {
            "provider_id": provider_id,
            "provider_title": "Kodik",
            "translation_id": translation_id,
            "translation_title": "New Voice",
            "embed_host": scrape_animego.embed_host(embed_url),
            "embed_url": embed_url,
            "embed_url_redacted": scrape_animego.redact_embed_url(embed_url),
        }

    def result_payload(self, anime_id, episode_id, *, provider=None, title="Client episode"):
        return {
            "anime_id": anime_id,
            "episodes": [
                {
                    "episode": {
                        "id": episode_id,
                        "number": "2",
                        "title": title,
                        "release_label": "today",
                        "episode_type": "episode",
                        "description": "new episode",
                    },
                    "providers": [provider or self.provider()],
                    "unavailable_reason": None,
                }
            ],
        }

    def create_job(self, mode="full", **kwargs):
        return animego_scans.create_scan_job(
            self.db_path,
            self.user,
            mode,
            origin="http://127.0.0.1:8765",
            **kwargs,
        )

    def listing_html(self, *ids):
        return "".join(
            f'<div class="ani-list__item"><div class="ani-list__item-title">'
            f'<a href="/anime/new-{i}">New {i}</a></div></div>' for i in ids
        )

    def discover(self, job, page, html=None, **extra):
        return animego_scans.discover_scan_titles(
            self.db_path, job["job"]["id"], job["token"],
            {"page": page, "html": html, **extra},
        )

    def test_discovery_creates_playable_draft_atomically_and_replays(self):
        job = self.create_job()
        self.assertEqual(job["job"]["status"], "running")
        listing = self.listing_html(701, 702)
        found = self.discover(job, 1, listing)
        self.assertEqual(len(found["tasks"]), 2)
        self.assertEqual(self.discover(job, 1, listing), found)
        with closing(server.connect(self.db_path)) as con:
            self.assertEqual(con.execute("select count(*) from anime").fetchone()[0], 0)
        body = self.result_payload(701, 70101)
        body["detail_html"] = '<h1>New title</h1><div class="description">Source description</div>'
        invalid = self.result_payload(701, 70101)
        invalid["detail_html"] = body["detail_html"]
        invalid["episodes"][0]["providers"][0]["embed_url"] = "https://invalid.example/video"
        with self.assertRaises(ValueError):
            animego_scans.submit_scan_result(self.db_path, job["job"]["id"], job["token"], invalid, server.PLAYER_HOSTS)
        first = animego_scans.submit_scan_result(self.db_path, job["job"]["id"], job["token"], body, server.PLAYER_HOSTS)
        self.assertEqual(first["result"]["new_episode_count"], 1)
        again = animego_scans.submit_scan_result(self.db_path, job["job"]["id"], job["token"], body, server.PLAYER_HOSTS)
        self.assertEqual(again["result"]["status"], "already_processed")
        animego_scans.submit_scan_result(self.db_path, job["job"]["id"], job["token"], {"anime_id": 702, "episodes": []}, server.PLAYER_HOSTS)
        with closing(server.connect(self.db_path)) as con:
            self.assertEqual(con.execute("select editorial_status from anime where id=701").fetchone()[0], "needs_review")
            self.assertEqual(con.execute("select description from anime where id=701").fetchone()[0], "Source description")
            self.assertIsNone(con.execute("select 1 from anime where id=702").fetchone())
            self.assertEqual(con.execute("select count(*) from video_sources").fetchone()[0], 1)
            self.assertEqual(con.execute("pragma foreign_key_check").fetchall(), [])
        item = server.catalog_api_item(server.get_catalog_items(self.db_path)[0])
        self.assertEqual(item["editorial_status"], "needs_review")
        self.assertTrue(self.discover(job, 2, not_found=True)["done"])
        result = animego_scans.complete_scan_job(self.db_path, job["job"]["id"], job["token"], {})
        self.assertEqual(result["job"]["checked_items"], 2)

    def test_discovery_continues_past_known_page_and_rejects_unparsed_page(self):
        self.add_title(1)
        job = self.create_job()
        self.assertFalse(self.discover(job, 1, self.listing_html(1))["done"])
        with self.assertRaisesRegex(ValueError, "no recognizable"):
            self.discover(job, 2, "<html>challenge</html>")
        self.assertEqual(len(self.discover(job, 2, self.listing_html(2))["tasks"]), 1)
        self.assertTrue(self.discover(job, 3, self.listing_html(2))["done"])
        with self.assertRaises(ValueError):
            animego_scans.complete_scan_job(self.db_path, job["job"]["id"], job["token"], {})

    def test_empty_listing_ends_discovery_but_challenge_does_not(self):
        job = self.create_job()
        self.discover(job, 1, self.listing_html(701))
        with self.assertRaisesRegex(ValueError, "no recognizable"):
            self.discover(job, 2, "<html>challenge</html>")
        self.assertTrue(self.discover(job, 2, '<div class="ani-list"></div>')["done"])

    def test_ready_metadata_is_preserved_by_automatic_import(self):
        con = scrape_animego.init_db(self.db_path)
        item = scrape_animego.parse_listing(self.listing_html(701))[0]
        detail = scrape_animego.parse_detail('<h1>Source title</h1>')
        scrape_animego.upsert_anime(con, item, detail, animego_scans.iso_timestamp(), authoritative_metadata=True)
        con.execute("update anime set editorial_status='ready', title='Edited title', description='Edited description' where id=701")
        scrape_animego.upsert_anime(con, item, detail, animego_scans.iso_timestamp(), authoritative_metadata=True)
        self.assertEqual(con.execute("select title, description, editorial_status from anime where id=701").fetchone(), ("Edited title", "Edited description", "ready"))
        con.close()

    def test_ready_release_facts_advance_without_erasing_editorial_work(self):
        con = scrape_animego.init_db(self.db_path)
        item = scrape_animego.parse_listing(self.listing_html(701))[0]
        detail = scrape_animego.parse_detail('<h1>Source title</h1>')
        detail['fields'] = {'Статус': 'Онгоинг', 'Эпизоды': '1 / 12'}
        scrape_animego.upsert_anime(con, item, detail, 'before', authoritative_metadata=True)
        con.execute("update anime set editorial_status='ready', title='Edited', description='Curated' where id=701")
        detail['fields'] = {'Статус': 'Вышел', 'Эпизоды': '12', 'Время': '24 мин.'}
        scrape_animego.upsert_anime(con, item, detail, 'after', authoritative_metadata=True)
        self.assertEqual(con.execute("select title,description,editorial_status,status,episodes_text,duration from anime where id=701").fetchone(), ('Edited', 'Curated', 'ready', 'Вышел', '12', '24 мин.'))
        self.assertEqual(json.loads(con.execute("select fields_json from anime where id=701").fetchone()[0])['Статус'], 'Вышел')
        detail['fields'] = {}
        scrape_animego.upsert_anime(con, item, detail, 'later', authoritative_metadata=True)
        self.assertEqual(con.execute("select status from anime where id=701").fetchone()[0], 'Вышел')
        con.close()

    def test_scan_refreshes_existing_title_without_new_episodes(self):
        self.add_title(701, playable=True)
        job = self.create_job()
        body = {'anime_id': 701, 'episodes': [], 'detail_html': '<h1>Source title</h1><div class="entity-field grid"><div class="text-body-tertiary">Статус</div><div class="text-break">Вышел</div></div>'}
        animego_scans.submit_scan_result(self.db_path, job['job']['id'], job['token'], body, server.PLAYER_HOSTS)
        with closing(server.connect(self.db_path)) as con:
            self.assertEqual(con.execute('select status from anime where id=701').fetchone()[0], 'Вышел')
            self.assertEqual(con.execute('select count(*) from episodes where anime_id=701').fetchone()[0], 1)

    def request(self, method, path, *, headers=None, body=None):
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.AnimeHandler)
        httpd.db_path = str(self.db_path)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=10)
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            response_body = response.read()
            return response.status, dict(response.getheaders()), response_body
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def session_token(self):
        con = server.connect(self.db_path)
        try:
            token, _ = server.create_session(con, self.user["id"])
            con.commit()
            return token
        finally:
            con.close()

    def test_partial_selection_combines_current_personal_and_weighted_random(self):
        for anime_id in range(1, 21):
            self.add_title(anime_id)
        self.set_library_state(2, watch_status="watching")
        self.set_library_state(3, favorite=True)

        con = server.connect(self.db_path)
        try:
            selected = animego_scans.select_scan_items(
                con,
                "partial",
                self.user["id"],
                current_anime_id=1,
                rng=random.Random(17),
            )
        finally:
            con.close()

        reasons = [reason for _, reason in selected]
        selected_ids = {anime_id for anime_id, _ in selected}
        self.assertEqual(len(selected), 11)
        self.assertEqual(selected[0], (1, "current"))
        self.assertTrue({2, 3} <= selected_ids)
        self.assertEqual(reasons.count("personal"), 2)
        self.assertEqual(reasons.count("random"), 8)

    def test_partial_checks_entire_library_even_completed_and_recently_checked(self):
        now = animego_scans.now_utc()
        for anime_id in range(1, 25):
            self.add_title(anime_id, status="Завершён", episodes_text="1", playable=True)
            self.set_library_state(anime_id, favorite=anime_id <= 12,
                                   watch_status="watching" if anime_id >= 12 else "not_watching")
        self.add_title(25, status="Завершён", episodes_text="1", playable=True)
        self.add_title(26, status="Завершён", episodes_text="1", playable=True)
        self.set_library_state(26)  # A state timestamp alone is not a personal subscription.
        for anime_id in range(100, 112):
            self.add_title(anime_id)
        with closing(server.connect(self.db_path)) as con:
            for anime_id in range(1, 25):
                con.execute("insert into animego_title_scan_state(anime_id,next_eligible_at) values (?,?)",
                            (anime_id, animego_scans.iso_timestamp(now + dt.timedelta(days=7))))
            con.commit()
            selected = animego_scans.select_scan_items(con, "partial", self.user["id"],
                                                       current_anime_id=25, rng=random.Random(7), now=now)
        self.assertEqual(selected[0], (25, "current"))
        self.assertEqual({i for i, reason in selected if reason == "personal"}, set(range(1, 25)))
        self.assertEqual(len(selected), 33)
        self.assertEqual(len({i for i, _ in selected}), 33)
        self.assertNotIn(26, dict(selected))

    def test_partial_rotation_uses_shared_successful_checks_and_excludes_completed(self):
        for anime_id in range(1, 21):
            self.add_title(anime_id)
        self.add_title(21, status="Завершён", placeholder=True)
        self.add_title(30_000_022, source="kinogoo", status="Завершён")
        first = self.create_job("partial", rng=random.Random(1))
        first_ids = {task["anime_id"] for task in first["tasks"]}
        self.assertEqual(len(first_ids), 8)
        self.assertTrue(first_ids <= set(range(1, 21)))
        for anime_id in first_ids:
            animego_scans.submit_scan_result(self.db_path, first["job"]["id"], first["token"],
                                            {"anime_id": anime_id, "episodes": []}, server.PLAYER_HOSTS)
        animego_scans.complete_scan_job(self.db_path, first["job"]["id"], first["token"], {})
        with closing(server.connect(self.db_path)) as con:
            second = animego_scans.select_scan_items(con, "partial", self.user["id"] + 1,
                                                     rng=random.Random(1))
        self.assertEqual(len(second), 8)
        self.assertTrue(first_ids.isdisjoint(dict(second)))

    def test_weighted_selection_favors_old_successes_over_recent_checks(self):
        now = animego_scans.now_utc()
        for anime_id in range(1, 33):
            self.add_title(anime_id)
        with closing(server.connect(self.db_path)) as con:
            for anime_id in range(1, 33):
                age = 30 if anime_id <= 16 else 1
                con.execute("insert into animego_title_scan_state(anime_id,last_checked_at) values (?,?)",
                            (anime_id, animego_scans.iso_timestamp(now - dt.timedelta(days=age))))
            con.commit()
            old_count = sum(i <= 16 for seed in range(100) for i, _ in
                            animego_scans.select_scan_items(con, "partial", self.user["id"],
                                                          now=now, rng=random.Random(seed)))
        self.assertGreater(old_count, 640)  # >80% from the older half, not uniform selection.

    def test_recent_content_changes_raise_weight_without_erasing_age_priority(self):
        now = animego_scans.now_utc()
        def state(days):
            return {"last_successful_at": animego_scans.iso_timestamp(now - dt.timedelta(days=days))}
        plain = {"last_content_change_at": None}
        updated = {"last_content_change_at": animego_scans.iso_timestamp(now)}
        self.assertGreater(animego_scans.partial_scan_weight(updated, state(5), now),
                           animego_scans.partial_scan_weight(plain, state(5), now))
        self.assertGreater(animego_scans.partial_scan_weight(plain, state(30), now),
                           animego_scans.partial_scan_weight(updated, state(1), now))

    def test_rotation_reads_content_events_but_not_new_card_or_metadata_timestamps(self):
        self.add_title(1)
        self.add_title(2)
        changed = animego_scans.iso_timestamp()
        with closing(server.connect(self.db_path)) as con:
            for anime_id, event in [(1, "new_episode"), (2, "new_title")]:
                con.execute("insert into content_update_events(event_type,anime_id,occurred_at,dedupe_key) values (?,?,?,?)",
                            (event, anime_id, changed, f"fixture-{anime_id}"))
            rows = {row["id"]: row for row in animego_scans.scan_candidate_rows(con)}
        self.assertEqual(rows[1]["last_content_change_at"], changed)
        self.assertIsNone(rows[2]["last_content_change_at"])

    def test_failed_attempt_does_not_become_successful_check(self):
        self.add_title(1)
        first = self.create_job("partial")
        animego_scans.submit_scan_result(self.db_path, first["job"]["id"], first["token"],
                                        {"anime_id": 1, "episodes": []}, server.PLAYER_HOSTS)
        animego_scans.complete_scan_job(self.db_path, first["job"]["id"], first["token"], {})
        with closing(server.connect(self.db_path)) as con:
            success = animego_scans.scan_state_by_anime_id(con, [1])[1]["last_successful_at"]
        later = animego_scans.now_utc() + dt.timedelta(days=2)
        second = self.create_job("partial", now=later)
        animego_scans.submit_scan_result(self.db_path, second["job"]["id"], second["token"],
                                        {"anime_id": 1, "error": "network failure"}, server.PLAYER_HOSTS, now=later)
        with closing(server.connect(self.db_path)) as con:
            state = animego_scans.scan_state_by_anime_id(con, [1])[1]
        self.assertEqual(state["last_successful_at"], success)
        self.assertFalse(animego_scans.timestamp_is_due(state["next_eligible_at"], later))

    def test_personal_variant_maps_to_animego_source(self):
        self.add_title(10, title="Canonical scanner title")
        self.add_title(
            10_000_010,
            title="Canonical scanner title",
            source="yummyanime",
            status="Завершён",
            episodes_text="1",
        )
        self.set_library_state(10_000_010, watch_status="watching")
        con = server.connect(self.db_path)
        try:
            selected = animego_scans.select_scan_items(
                con,
                "partial",
                self.user["id"],
                variant_map={10_000_010: 10},
                rng=random.Random(1),
            )
        finally:
            con.close()
        self.assertIn((10, "personal"), selected)

    def test_full_selection_includes_completed_and_rechecks_known_players(self):
        self.add_title(1, playable=True)
        self.add_title(2, status="Завершён", placeholder=True)
        self.add_title(3, status="Завершён", episodes_text="1", playable=True)
        con = server.connect(self.db_path)
        try:
            selected = animego_scans.select_scan_items(
                con, "full", self.user["id"]
            )
        finally:
            con.close()
        self.assertEqual({anime_id for anime_id, _ in selected}, {1, 2, 3})
        self.assertTrue(all(reason == "full" for _, reason in selected))
        with closing(server.connect(self.db_path)) as con:
            self.assertTrue(all(not task["known_episode_ids"] for task in animego_scans.task_payloads(con, selected)))

    def test_partial_excludes_old_completed_even_when_personal_or_current(self):
        now = dt.datetime(2026, 10, 16, tzinfo=dt.timezone.utc)
        for anime_id, status, release in [
            (1, "Вышел", "с 10 января 2025 по 4 июля 2025"),
            (2, "Завершён", "с 1 января 2026 по 16 сентября 2026"),
            (3, "Вышел", "с 1 января 2026"),
            (4, "Онгоинг", "с 1 января 2025 по 4 июля 2025"),
            (5, "Выпуск завершён", "с 1 января 2025 по 4 июля 2025"),
        ]:
            self.add_title(anime_id, status=status, source="kinogoo" if anime_id == 5 else "animego")
            self.set_library_state(anime_id, watch_status="watching")
            with closing(server.connect(self.db_path)) as con:
                con.execute("insert into anime_fields(anime_id,label,value) values (?, 'Выпуск', ?)", (anime_id, release))
                con.commit()
        with closing(server.connect(self.db_path)) as con:
            selected = animego_scans.select_scan_items(con, "partial", self.user["id"], current_anime_id=1, now=now)
            full = animego_scans.select_scan_items(con, "full", self.user["id"], now=now)
        self.assertEqual({i for i, _ in selected}, {2, 3, 4})
        self.assertEqual({i for i, _ in full}, {1, 2, 3, 4, 5})

    def test_partial_handles_completed_release_formats_from_job_140(self):
        now = dt.datetime(2026, 10, 17, tzinfo=dt.timezone.utc)
        cases = [
            ("2021", "Сериал", True),
            ("2022–2023", "Сериал", True),
            ("2022 - 2023", "Сериал", True),
            ("2022—2023", "Сериал", True),
            ("27 февраля 2020", "Спешл", True),
            ("27 марта 2024", "Спешл", True),
            ("6 октября 2023", "Фильм", True),
            ("27 февраля 2026", "Фильм", True),
            ("с 4 июля 2026 по 19 сентября 2026", "TV Сериал", False),
            ("17 сентября 2026", "Фильм", False),
            ("16 сентября 2026", "Фильм", True),
            ("2026", "Сериал", False),
            ("2027", "Сериал", False),
            ("2023–2022", "Сериал", False),
            ("0000", "Сериал", False),
            ("2022–", "Сериал", False),
            ("с 1 января 2020", "Спешл", False),
            ("1 января 2020", "TV Сериал", False),
            ("31 февраля 2020", "Фильм", False),
            ("", "Фильм", False),
        ]
        retained = set()
        for anime_id, (release, kind, excluded) in enumerate(cases, 1):
            self.add_title(anime_id, status="Вышел")
            self.set_library_state(anime_id, watch_status="watching")
            with closing(server.connect(self.db_path)) as con:
                con.execute("update anime set kind=? where id=?", (kind, anime_id))
                con.execute("insert into anime_fields(anime_id,label,value) values (?, 'Выпуск', ?)", (anime_id, release))
                con.commit()
            if not excluded:
                retained.add(anime_id)
        with closing(server.connect(self.db_path)) as con:
            partial = animego_scans.select_scan_items(con, "partial", self.user["id"], current_anime_id=1, now=now)
            full = animego_scans.select_scan_items(con, "full", self.user["id"], now=now)
        self.assertEqual({i for i, _ in partial}, retained)
        self.assertEqual(len(full), len(cases))
        for release in ["2021", "2022–2023", "27 февраля 2020"]:
            self.assertFalse(animego_scans.completed_over_month_ago(
                {"status": "Онгоинг", "release_period": release, "kind": "Фильм"}, now))
        row = {"status": "Выпуск завершён", "release_period": "2025", "kind": "Сериал"}
        self.assertFalse(animego_scans.completed_over_month_ago(row, dt.datetime(2026, 1, 30)))
        self.assertTrue(animego_scans.completed_over_month_ago(row, dt.datetime(2026, 1, 31)))

    def test_global_lease_conflicts_until_job_completes(self):
        self.add_title(1)
        first = self.create_job()
        with self.assertRaises(animego_scans.ScanConflictError):
            self.create_job()
        with self.assertRaisesRegex(ValueError, "pending titles"):
            animego_scans.complete_scan_job(
                self.db_path, first["job"]["id"], first["token"], {}
            )
        still_running = animego_scans.get_scan_job(self.db_path, first["job"]["id"], first["token"])
        self.assertEqual(still_running["job"]["status"], "running")
        animego_scans.submit_scan_result(
            self.db_path, first["job"]["id"], first["token"],
            {"anime_id": 1, "episodes": []}, server.PLAYER_HOSTS,
        )
        self.discover(first, 1, self.listing_html(1))
        self.discover(first, 2, not_found=True)
        completed = animego_scans.complete_scan_job(
            self.db_path, first["job"]["id"], first["token"], {}
        )
        self.assertEqual(completed["job"]["status"], "completed")
        repeated = animego_scans.complete_scan_job(
            self.db_path, first["job"]["id"], first["token"], {}
        )
        self.assertEqual(repeated["job"], completed["job"])
        second = self.create_job()
        self.assertEqual(second["job"]["status"], "running")

    def test_zero_work_job_completes_immediately_without_holding_lease(self):
        self.add_title(1, status="Завершён", episodes_text="1", playable=True)
        first = self.create_job("partial")
        self.assertEqual(first["tasks"], [])
        self.assertEqual(first["job"]["status"], "completed")
        self.assertEqual(first["job"]["total_items"], 0)
        second = self.create_job("partial")
        self.assertEqual(second["job"]["status"], "completed")
        con = server.connect(self.db_path)
        try:
            statuses = [
                row[0]
                for row in con.execute(
                    "select status from content_update_runs order by id"
                ).fetchall()
            ]
        finally:
            con.close()
        self.assertEqual(statuses[-2:], ["success", "success"])

    def test_new_episode_is_additive_attributed_idempotent_and_emits_update(self):
        self.add_title(5)
        job = self.create_job()
        payload = self.result_payload(5, 502)
        result = animego_scans.submit_scan_result(
            self.db_path,
            job["job"]["id"],
            job["token"],
            payload,
            server.PLAYER_HOSTS,
        )
        self.assertEqual(result["result"]["new_episode_count"], 1)
        self.assertEqual(result["result"]["new_provider_count"], 1)

        repeated = animego_scans.submit_scan_result(
            self.db_path,
            job["job"]["id"],
            job["token"],
            payload,
            server.PLAYER_HOSTS,
        )
        self.assertEqual(repeated["result"]["status"], "already_processed")

        con = server.connect(self.db_path)
        try:
            episode = con.execute("select * from episodes where id = 502").fetchone()
            attribution = con.execute(
                "select * from animego_episode_additions where source_episode_id = 502"
            ).fetchone()
            provider_attribution = con.execute(
                "select * from animego_provider_additions where source_episode_id = 502"
            ).fetchone()
            event = con.execute(
                "select * from content_update_events where anime_id = 5 and episode_id = 502"
            ).fetchone()
            counts = con.execute(
                "select count(*) from video_sources where episode_id = 502"
            ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(episode["anime_id"], 5)
        self.assertEqual(attribution["user_id"], self.user["id"])
        self.assertEqual(provider_attribution["user_id"], self.user["id"])
        self.assertEqual(event["event_type"], "new_episode")
        self.assertEqual(counts, 1)

    def test_placeholder_provider_fill_only_makes_episode_playable_and_attributed(self):
        self.add_title(7, status="Завершён", placeholder=True)
        job = self.create_job()
        provider = self.provider(provider_id="provider-7", translation_id=1)
        provider["provider_title"] = "Changed provider title"
        provider["translation_title"] = "Changed translation"
        result = animego_scans.submit_scan_result(
            self.db_path,
            job["job"]["id"],
            job["token"],
            self.result_payload(7, 701, provider=provider, title="Changed episode title"),
            server.PLAYER_HOSTS,
        )
        self.assertEqual(result["result"]["new_episode_count"], 1)
        self.assertEqual(result["result"]["new_provider_count"], 1)

        con = server.connect(self.db_path)
        try:
            episode = con.execute("select * from episodes where id = 701").fetchone()
            source = con.execute(
                "select * from video_sources where episode_id = 701"
            ).fetchone()
            episode_audit = con.execute(
                "select * from animego_episode_additions where source_episode_id = 701"
            ).fetchone()
            provider_audit = con.execute(
                "select * from animego_provider_additions where source_episode_id = 701"
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(episode["title"], "Original episode title")
        self.assertEqual(episode["has_video"], 1)
        self.assertIsNone(episode["unavailable_reason"])
        self.assertEqual(source["provider_title"], "Original provider")
        self.assertEqual(source["translation_title"], "Dream Cast")
        self.assertEqual(source["embed_url"], provider["embed_url"])
        self.assertEqual(episode_audit["user_id"], self.user["id"])
        self.assertEqual(provider_audit["video_source_id"], source["id"])

    def test_existing_playable_episode_metadata_is_not_overwritten(self):
        self.add_title(8, playable=True)
        job = self.create_job()
        result = animego_scans.submit_scan_result(
            self.db_path,
            job["job"]["id"],
            job["token"],
            self.result_payload(8, 801, title="Changed by client"),
            server.PLAYER_HOSTS,
        )
        self.assertEqual(result["result"]["new_episode_count"], 0)
        self.assertEqual(result["result"]["new_provider_count"], 1)
        con = server.connect(self.db_path)
        try:
            episode = con.execute("select * from episodes where id = 801").fetchone()
        finally:
            con.close()
        self.assertEqual(episode["title"], "Original episode title")

    def test_full_refreshes_existing_provider_without_counting_it_as_new(self):
        self.add_title(8, playable=True)
        job = self.create_job()
        provider = self.provider("provider-8", 1)
        result = animego_scans.submit_scan_result(
            self.db_path, job["job"]["id"], job["token"],
            self.result_payload(8, 801, provider=provider), server.PLAYER_HOSTS,
        )
        self.assertEqual(result["result"]["new_episode_count"], 0)
        self.assertEqual(result["result"]["new_provider_count"], 0)
        with closing(server.connect(self.db_path)) as con:
            row = con.execute("select embed_url from video_sources where episode_id=801").fetchone()
        self.assertEqual(row[0], provider["embed_url"])

    def test_unsafe_provider_is_rejected_without_consuming_item(self):
        self.add_title(9)
        job = self.create_job()
        provider = self.provider()
        provider.update(
            {
                "embed_host": "evil.example",
                "embed_url": "https://evil.example/player/token",
                "embed_url_redacted": "//evil.example/player/<redacted>",
            }
        )
        with self.assertRaisesRegex(ValueError, "allowed HTTPS"):
            animego_scans.submit_scan_result(
                self.db_path,
                job["job"]["id"],
                job["token"],
                self.result_payload(9, 902, provider=provider),
                server.PLAYER_HOSTS,
            )
        con = server.connect(self.db_path)
        try:
            item = con.execute(
                "select * from animego_scan_job_items where job_id = ? and anime_id = 9",
                (job["job"]["id"],),
            ).fetchone()
            episode = con.execute("select 1 from episodes where id = 902").fetchone()
        finally:
            con.close()
        self.assertEqual(item["status"], "pending")
        self.assertIsNone(episode)

    def test_complete_stop_records_summary_errors_without_counting_them_checked(self):
        self.add_title(10)
        self.add_title(11)
        job = self.create_job()
        failed = animego_scans.submit_scan_result(
            self.db_path,
            job["job"]["id"],
            job["token"],
            {"anime_id": 10, "episodes": [], "error": "title fetch failed"},
            server.PLAYER_HOSTS,
        )
        self.assertEqual(failed["job"]["checked_items"], 1)
        stopped = animego_scans.complete_scan_job(
            self.db_path,
            job["job"]["id"],
            job["token"],
            {"stopped": True, "errors": [{"anime_id": 11, "message": "stopped by user"}]},
        )
        self.assertEqual(stopped["job"]["status"], "stopped")
        self.assertEqual(stopped["job"]["checked_items"], 1)
        self.assertEqual(stopped["job"]["error_count"], 2)
        con = server.connect(self.db_path)
        try:
            rows = {
                row["anime_id"]: row
                for row in con.execute(
                    "select * from animego_scan_job_items where job_id = ?",
                    (job["job"]["id"],),
                ).fetchall()
            }
        finally:
            con.close()
        self.assertIsNotNone(rows[10]["checked_at"])
        self.assertIsNone(rows[11]["checked_at"])
        self.assertEqual(rows[11]["status"], "failed")

    def test_extension_zip_is_deterministic_and_excludes_hidden_files(self):
        extension_root = Path(self.tmpdir.name) / "extension"
        extension_root.mkdir()
        (extension_root / "manifest.json").write_text('{"manifest_version":3}', encoding="utf-8")
        (extension_root / "worker.js").write_text("export {};", encoding="utf-8")
        (extension_root / ".secret").write_text("no", encoding="utf-8")
        first = animego_scans.build_extension_zip(extension_root)
        second = animego_scans.build_extension_zip(extension_root)
        self.assertEqual(first, second)
        with zipfile.ZipFile(io.BytesIO(first)) as archive:
            self.assertEqual(
                archive.namelist(),
                ["animego-scanner/manifest.json", "animego-scanner/worker.js"],
            )

    def test_http_create_status_setup_and_extension_download_auth(self):
        self.add_title(12)
        session = self.session_token()
        cookie = {"Cookie": f"{server.SESSION_COOKIE_NAME}={session}"}
        create_body = json.dumps({"mode": "full"}).encode("utf-8")
        status, _, raw = self.request(
            "POST",
            "/api/animego-scans",
            headers={**cookie, "Content-Type": "application/json"},
            body=create_body,
        )
        self.assertEqual(status, 201)
        created = json.loads(raw)
        self.assertEqual(created["tasks"][0]["selection_reason"], "full")
        self.assertTrue(created["origin"].startswith("http://127.0.0.1:"))

        status, _, raw = self.request(
            "GET",
            f"/api/animego-scans/{created['job']['id']}",
            headers={"Authorization": f"Bearer {created['token']}"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["job"]["status"], "running")

        anonymous_setup, headers, _ = self.request("GET", "/scanner-setup")
        self.assertEqual(anonymous_setup, 302)
        self.assertTrue(headers["Location"].startswith("/login?next="))
        authenticated_setup, _, setup_body = self.request(
            "GET", "/scanner-setup", headers=cookie
        )
        self.assertEqual(authenticated_setup, 200)
        self.assertIn(b"AnimeGo", setup_body)
        self.assertIn(b"Safari 26", setup_body)
        self.assertIn(b"Add Temporary Extension", setup_body)

        anonymous_zip, _, _ = self.request("GET", "/api/animego-scanner-extension")
        self.assertEqual(anonymous_zip, 401)
        zip_status, zip_headers, zip_body = self.request(
            "GET", "/api/animego-scanner-extension", headers=cookie
        )
        self.assertEqual(zip_status, 200)
        self.assertEqual(zip_headers["Content-Type"], "application/zip")
        self.assertEqual(
            zip_headers["Content-Disposition"],
            'attachment; filename="animego-scanner.zip"',
        )
        with zipfile.ZipFile(io.BytesIO(zip_body)) as archive:
            self.assertIn("animego-scanner/manifest.json", archive.namelist())
            manifest = json.loads(archive.read("animego-scanner/manifest.json"))
        source_manifest = json.loads((server.SCANNER_EXTENSION_DIR / "manifest.json").read_text())
        self.assertEqual(manifest["version"], source_manifest["version"])
        self.assertEqual(
                    manifest["optional_host_permissions"], ["https://api.yani.tv/*", "https://animego.me/*", "https://kinogoo.fm/*", "https://api.nextembed.ws/*"]
        )
        self.assertNotIn("https://animego.me/*", manifest["host_permissions"])
        self.assertEqual(
            manifest["browser_specific_settings"]["safari"]["strict_min_version"],
            "16.4",
        )


if __name__ == "__main__":
    unittest.main()
