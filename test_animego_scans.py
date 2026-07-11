#!/usr/bin/env python3
import http.client
import io
import json
from pathlib import Path
import random
import tempfile
import threading
import unittest
import zipfile

import animego_scans
import scrape_animego
import server


class AnimeGoScansTest(unittest.TestCase):
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

    def test_partial_selection_combines_current_personal_stale_and_random(self):
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
        self.assertEqual(len(selected), 15)
        self.assertEqual(selected[0], (1, "current"))
        self.assertTrue({2, 3} <= selected_ids)
        self.assertEqual(reasons.count("personal"), 2)
        self.assertEqual(reasons.count("random"), 3)
        self.assertGreaterEqual(reasons.count("stale"), 5)

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

    def test_full_selection_includes_ongoing_and_missing_playable_only(self):
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
        self.assertEqual({anime_id for anime_id, _ in selected}, {1, 2})
        self.assertTrue(all(reason == "full" for _, reason in selected))

    def test_global_lease_conflicts_until_job_completes(self):
        self.add_title(1)
        first = self.create_job()
        with self.assertRaises(animego_scans.ScanConflictError):
            self.create_job()
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
        first = self.create_job()
        self.assertEqual(first["tasks"], [])
        self.assertEqual(first["job"]["status"], "completed")
        self.assertEqual(first["job"]["total_items"], 0)
        second = self.create_job()
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


if __name__ == "__main__":
    unittest.main()
