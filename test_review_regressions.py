import datetime as dt
import json
import unittest
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import patch

import content_updates
import server
import sync_videos
import test_animego_scans
import test_pipeline_hardening


class ReviewRegressionTest(unittest.TestCase):
    def setUp(self):
        self.fixture = test_animego_scans.AnimeGoScansTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_known_providers_do_not_prevent_metadata_refresh(self):
        snapshot = test_pipeline_hardening.PipelineHardeningTest.animego_bundle()["snapshots"][0]
        args = SimpleNamespace(
            refresh_known=False, include_empty_episodes=False, dry_run=False,
            scraped_at=server.now_iso(), content_update_run_id=None,
        )
        con = server.connect(self.fixture.db_path)
        try:
            sync_videos.apply_animego_snapshot(con, snapshot, args, defaultdict(int), "ongoing")
            con.commit()
            before = tuple(con.execute("select count(*) from video_sources").fetchone())
            snapshot["detail"]["fields"]["Статус"] = "Завершён"
            snapshot["detail"]["description"] = "Updated description"
            stats = defaultdict(int)
            sync_videos.apply_animego_snapshot(con, snapshot, args, stats, "ongoing")
            con.commit()
            row = con.execute("select status, description from anime where id = ?", (snapshot["item"]["id"],)).fetchone()
            self.assertEqual(tuple(row), ("Завершён", "Updated description"))
            self.assertEqual(tuple(con.execute("select count(*) from video_sources").fetchone()), before)
            self.assertEqual(stats["metadata_refreshed"], 1)
            snapshot["item"]["id"] = 9000
            snapshot["episodes"] = []
            sync_videos.apply_animego_snapshot(con, snapshot, args, defaultdict(int), "ongoing")
            self.assertIsNone(con.execute("select 1 from anime where id = 9000").fetchone())
        finally:
            con.close()

    def test_recent_badges_expire_without_catalog_writes(self):
        self.fixture.add_title(100, playable=True)
        occurred = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        con = server.connect(self.fixture.db_path)
        try:
            con.execute(
                "insert into content_update_events(event_type, anime_id, occurred_at, dedupe_key) values ('new_episode', 100, ?, 'expiry-test')",
                (occurred.isoformat(),),
            )
            con.commit()
        finally:
            con.close()
        before = server.get_anime_list(self.fixture.db_path)[0]
        self.assertIsNotNone(before["recent_update_summary"])
        later = occurred + dt.timedelta(days=7, seconds=2)
        with (
            patch.object(server.time, "time", return_value=later.timestamp()),
            patch.object(content_updates, "recent_cutoff", return_value=(later - dt.timedelta(days=7)).isoformat()),
        ):
            after = server.get_anime_list(self.fixture.db_path)[0]
            self.assertIsNone(after["recent_update_summary"])
            self.assertEqual(after["recent_updates"], [])
            detail = server.get_anime_detail(100, self.fixture.db_path)
            self.assertIsNone(detail["recent_update_summary"])

    def test_json_login_requires_matching_browser_state_before_token_verification(self):
        binding = "a" * 43
        signed = server.sign_google_auth_state("/", binding)
        for state, cookie in ((signed, ""), (signed, "b" * 43), ("", binding), ([], binding)):
            with self.subTest(state=bool(state), cookie_matches=cookie == binding):
                with patch.object(server, "authenticate_google_credential") as authenticate:
                    status, _, _ = self.fixture.request(
                        "POST", "/api/auth/google",
                        headers={"Content-Type": "application/json", "Cookie": f"{server.LOGIN_BROWSER_COOKIE_NAME}={cookie}"},
                        body=json.dumps({"credential": "fake", "state": state}),
                    )
                self.assertEqual(status, 401)
                authenticate.assert_not_called()

    def test_handoff_rejects_cross_browser_navigation_without_consuming_code(self):
        binding = "a" * 43
        code = server.create_login_handoff(
            self.fixture.session_token(), "/", self.fixture.db_path,
            browser_binding_hash=server.session_token_hash(binding),
        )
        path = f"/api/auth/complete?code={code}"
        for cookie in ("", "b" * 43):
            status, headers, _ = self.fixture.request(
                "GET", path,
                headers={"Sec-Fetch-Site": "cross-site", "Cookie": f"{server.LOGIN_BROWSER_COOKIE_NAME}={cookie}"},
            )
            self.assertEqual(status, 302)
            self.assertNotIn("Set-Cookie", headers)
        status, headers, _ = self.fixture.request(
            "GET", path, headers={"Cookie": f"{server.LOGIN_BROWSER_COOKIE_NAME}={binding}"},
        )
        self.assertEqual(status, 200)
        self.assertIn(server.SESSION_COOKIE_NAME, headers["Set-Cookie"])
        status, _, _ = self.fixture.request(
            "GET", path, headers={"Cookie": f"{server.LOGIN_BROWSER_COOKIE_NAME}={binding}"},
        )
        self.assertEqual(status, 302)

    def test_access_log_does_not_include_query_secrets(self):
        handler = object.__new__(server.AnimeHandler)
        handler.path = "/api/auth/complete?code=secret-login-code&credential=secret"
        handler.command = "GET"
        handler.client_address = ("127.0.0.1", 1234)
        with patch.object(server, "server_logger") as logger:
            handler.log_message('"%s" %s %s', f"GET {handler.path} HTTP/1.1", "200", "-")
        args = logger.return_value.info.call_args.args
        logged = args[0] % args[1:]
        self.assertIn("/api/auth/complete", logged)
        self.assertNotIn("secret", logged)
        self.assertNotIn("?", logged)

    def test_auth_config_preserves_browser_binding_across_tabs(self):
        binding = "a" * 43
        with patch.object(server, "google_client_id", return_value="test-client"):
            status, headers, body = self.fixture.request(
                "GET", "/api/auth/config",
                headers={"Cookie": f"{server.LOGIN_BROWSER_COOKIE_NAME}={binding}"},
            )
        self.assertEqual(status, 200)
        self.assertIn(binding, headers["Set-Cookie"])
        self.assertEqual(server.verify_google_auth_state(json.loads(body)["state"], binding), "/")
