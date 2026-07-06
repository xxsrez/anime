#!/usr/bin/env python3
import http.client
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlencode, urlparse
from unittest.mock import patch

import server


class LocalAppTest(unittest.TestCase):
    def create_google_user(self, db_path, sub, email):
        con = server.connect(db_path)
        try:
            user = server.upsert_google_user(
                con,
                {
                    "sub": sub,
                    "email": email,
                    "email_verified": True,
                    "name": email.split("@")[0],
                    "picture": None,
                },
            )
            con.commit()
            return user["id"]
        finally:
            con.close()

    def create_session(self, db_path, user_id):
        con = server.connect(db_path)
        try:
            token, _ = server.create_session(con, user_id)
            con.commit()
            return token
        finally:
            con.close()

    def request_test_server(self, db_path, method, path, headers=None, body=None):
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.AnimeHandler)
        httpd.db_path = db_path
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            body = response.read()
            return response.status, dict(response.getheaders()), body
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def source_row(self, translation, provider="Kodik", episode_id=1, source="animego", row_id=1):
        return {
            "id": row_id,
            "episode_id": episode_id,
            "source": source,
            "translation_title": translation,
            "provider_title": provider,
            "embed_host": "kodikplayer.com" if provider.startswith("Kodik") else "example.test",
        }

    def test_catalog_has_scraped_titles(self):
        items = server.get_anime_list()
        self.assertGreaterEqual(len(items), 10)
        self.assertTrue(all(item["source_count"] > 0 for item in items))
        self.assertTrue(all(item["available_episode_count"] > 0 for item in items))
        slugs = [item["slug"] for item in items]
        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertTrue(all(item["internal_id"] == item["slug"] for item in items))
        self.assertTrue(all("-" in item["slug"] for item in items))

    def test_title_detail_can_be_loaded_by_slug(self):
        item = server.get_anime_list()[0]
        detail = server.get_anime_detail(item["slug"])
        self.assertIsNotNone(detail)
        self.assertEqual(detail["id"], item["id"])
        self.assertEqual(detail["slug"], item["slug"])
        self.assertEqual(detail["internal_id"], item["slug"])

    def test_detail_contains_episode_sources(self):
        anime = next(item for item in server.get_anime_list() if item["source_count"] > 0)
        detail = server.get_anime_detail(anime["id"])
        self.assertIsNotNone(detail)
        self.assertGreater(len(detail["episodes"]), 0)
        source_rows = [
            source
            for sources in detail["sources_by_episode"].values()
            for source in sources
        ]
        self.assertTrue(any(source["embed_url"] for source in source_rows))
        self.assertTrue(any(source["translation_title"] for source in source_rows))

    def test_source_sort_pins_dream_cast_above_popular_translations(self):
        sources = [
            self.source_row("AniStar", row_id=1),
            self.source_row("Dreamcast", row_id=2),
            self.source_row("AniDUB", row_id=3),
        ]
        rankings = {
            "anistar": {"rank": 0},
            "anidub": {"rank": 1},
            "dream cast": {"rank": 20},
        }
        context = server.build_source_ranking_context({1: sources}, rankings)

        sorted_sources = sorted(sources, key=lambda source: server.source_row_sort_key(source, context))

        self.assertEqual(sorted_sources[0]["translation_title"], "Dreamcast")

    def test_source_sort_demotes_subtitles_and_generic_labels(self):
        sources = [
            self.source_row("YummyAnime", row_id=1, source="yummyanime"),
            self.source_row("Субтитры", row_id=2),
            self.source_row("AnimeVost", row_id=3),
        ]
        rankings = {
            "субтитры": {"rank": 0},
            "animevost": {"rank": 5},
        }
        context = server.build_source_ranking_context({1: sources}, rankings)

        sorted_sources = sorted(sources, key=lambda source: server.source_row_sort_key(source, context))

        self.assertEqual([source["translation_title"] for source in sorted_sources], ["AnimeVost", "Субтитры", "YummyAnime"])

    def test_source_sort_prefers_title_wide_translation_coverage(self):
        episode_one = [
            self.source_row("AniStar", episode_id=1, row_id=1),
            self.source_row("SHIZA Project", episode_id=1, row_id=2),
        ]
        episode_two = [
            self.source_row("SHIZA Project", episode_id=2, row_id=3),
        ]
        rankings = {
            "anistar": {"rank": 0},
            "shiza project": {"rank": 10},
        }
        context = server.build_source_ranking_context({1: episode_one, 2: episode_two}, rankings)

        sorted_sources = sorted(episode_one, key=lambda source: server.source_row_sort_key(source, context))

        self.assertEqual(sorted_sources[0]["translation_title"], "SHIZA Project")

    def test_translation_rankings_exclude_generic_source_labels(self):
        con = server.connect()
        try:
            rankings = server.build_translation_rankings(con)
        finally:
            con.close()

        self.assertIn("dream cast", rankings)
        self.assertNotIn("yummyanime", rankings)

    def test_year_metadata_fields_are_available(self):
        items = server.get_anime_list()
        for year in ("2025", "2026"):
            with self.subTest(year=year):
                self.assertGreaterEqual(
                    sum(1 for item in items if item.get("year") == year or str(item.get("date_published") or "").startswith(year)),
                    100,
                )
        scored = [item for item in items if item.get("aggregate_score")]
        self.assertGreaterEqual(len(scored), 200)
        detail = server.get_anime_detail(scored[0]["id"])
        labels = {field["label"] for field in detail["fields"]}
        self.assertIn("Тип", labels)
        self.assertIn("Статус", labels)

    def test_effective_rating_prefers_external_rating_over_tiny_local_vote_count(self):
        items = server.get_anime_list()
        wixoss = next(item for item in items if item["id"] == 10005287)

        self.assertEqual(wixoss["aggregate_score"], 10.0)
        self.assertEqual(wixoss["aggregate_count"], 2)
        self.assertAlmostEqual(wixoss["external_score"], 5.6)
        self.assertEqual(wixoss["external_score_source"], "Shikimori")
        self.assertAlmostEqual(wixoss["effective_score"], 5.6)

        ranked = sorted(items, key=lambda item: (-(server.best_score(item) or 0), item["title"]))
        self.assertNotEqual(ranked[0]["id"], wixoss["id"])

    def test_user_state_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)

            anime_id = server.get_anime_list(db_path)[0]["id"]
            saved = server.update_user_state(
                anime_id,
                {"is_favorite": True, "progress_episode_number": 7, "watched": False},
                db_path,
            )
            self.assertTrue(saved["is_favorite"])
            self.assertEqual(saved["progress_episode_number"], 7)
            self.assertFalse(saved["watched"])

            item = next(item for item in server.get_anime_list(db_path) if item["id"] == anime_id)
            self.assertTrue(item["is_favorite"])
            self.assertEqual(item["progress_episode_number"], 7)
            self.assertFalse(item["watched"])

            server.update_user_state(anime_id, {"watched": True}, db_path)
            detail = server.get_anime_detail(anime_id, db_path)
            self.assertTrue(detail["is_favorite"])
            self.assertEqual(detail["progress_episode_number"], 7)
            self.assertTrue(detail["watched"])

    def test_api_requires_authenticated_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)

            status, _, _ = self.request_test_server(db_path, "GET", "/api/anime")
            self.assertEqual(status, 401)

            status, headers, _ = self.request_test_server(db_path, "GET", "/")
            self.assertEqual(status, 302)
            self.assertTrue(headers["Location"].startswith("/login?next="))

            user_id = self.create_google_user(db_path, "google-user-1", "one@example.com")
            token = self.create_session(db_path, user_id)
            status, _, body = self.request_test_server(
                db_path,
                "GET",
                "/api/me",
                headers={"Cookie": f"{server.SESSION_COOKIE_NAME}={token}"},
            )
            self.assertEqual(status, 200)
            self.assertIn(b"one@example.com", body)

    def test_google_redirect_post_sets_session_cookie_on_completion_page(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)
            body = urlencode(
                {
                    "credential": "fake-token",
                    "state": server.sign_google_auth_state("/some-title"),
                }
            )
            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
            }
            auth = {
                "user": {"id": 10, "email": "one@example.com", "name": "One", "picture_url": None},
                "token": "session-token",
                "expires_at": "2030-01-01T00:00:00+00:00",
            }

            with patch.object(server, "authenticate_google_credential", return_value=auth) as authenticate:
                status, headers, _ = self.request_test_server(
                    db_path,
                    "POST",
                    "/api/auth/google",
                    headers=headers,
                    body=body,
                )

            self.assertEqual(status, 302)
            complete_location = headers["Location"]
            self.assertEqual(urlparse(complete_location).path, "/api/auth/complete")
            self.assertNotIn("Set-Cookie", headers)
            authenticate.assert_called_once_with("fake-token", db_path)

            status, headers, response_body = self.request_test_server(
                db_path,
                "GET",
                complete_location,
            )

            self.assertEqual(status, 200)
            self.assertIn(f"{server.SESSION_COOKIE_NAME}=session-token", headers["Set-Cookie"])
            self.assertIn(b"waitForSession", response_body)
            self.assertIn(b'fetch("/api/me"', response_body)
            self.assertIn(b'credentials: "same-origin"', response_body)
            self.assertIn(b"window.location.replace", response_body)
            self.assertIn(b"/some-title", response_body)
            self.assertIn(b"auth_complete", response_body)

            status, headers, _ = self.request_test_server(
                db_path,
                "GET",
                complete_location,
            )

            self.assertEqual(status, 302)
            self.assertEqual(urlparse(headers["Location"]).path, "/login")

    def test_google_json_post_returns_completion_url_without_fetch_cookie(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)
            body = json.dumps({"credential": "fake-token", "next": "/wanted"})
            headers = {
                "Content-Type": "application/json",
            }
            auth = {
                "user": {"id": 10, "email": "one@example.com", "name": "One", "picture_url": None},
                "token": "session-token",
                "expires_at": "2030-01-01T00:00:00+00:00",
            }

            with patch.object(server, "authenticate_google_credential", return_value=auth) as authenticate:
                status, headers, response_body = self.request_test_server(
                    db_path,
                    "POST",
                    "/api/auth/google",
                    headers=headers,
                    body=body,
                )

            self.assertEqual(status, 200)
            self.assertNotIn("Set-Cookie", headers)
            payload = json.loads(response_body)
            self.assertEqual(payload["user"]["email"], "one@example.com")
            self.assertEqual(urlparse(payload["complete_url"]).path, "/api/auth/complete")
            authenticate.assert_called_once_with("fake-token", db_path)

            status, headers, response_body = self.request_test_server(
                db_path,
                "GET",
                payload["complete_url"],
            )

            self.assertEqual(status, 200)
            self.assertIn(f"{server.SESSION_COOKIE_NAME}=session-token", headers["Set-Cookie"])
            self.assertIn(b"waitForSession", response_body)
            self.assertIn(b"/wanted", response_body)
            self.assertIn(b"auth_complete", response_body)

    def test_auth_config_returns_signed_google_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)

            with patch.object(server, "google_client_id", return_value="client.apps.googleusercontent.com"):
                status, _, body = self.request_test_server(
                    db_path,
                    "GET",
                    "/api/auth/config?next=%2Fwanted%3Ftab%3Dfavorites",
                )

            self.assertEqual(status, 200)
            payload = json.loads(body)
            self.assertEqual(payload["client_id"], "client.apps.googleusercontent.com")
            self.assertTrue(payload["state"])
            self.assertEqual(server.verify_google_auth_state(payload["state"]), "/wanted?tab=favorites")

    def test_google_auth_complete_rejects_invalid_handoff_code(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)

            status, headers, _ = self.request_test_server(
                db_path,
                "GET",
                "/api/auth/complete?code=missing",
            )

            self.assertEqual(status, 302)
            self.assertEqual(urlparse(headers["Location"]).path, "/login")
            self.assertIn("auth_error", parse_qs(urlparse(headers["Location"]).query))

    def test_google_redirect_post_redirects_invalid_state_to_login(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)
            body = urlencode({"credential": "fake-token", "state": "invalid-state"})
            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
            }

            status, headers, response_body = self.request_test_server(
                db_path,
                "POST",
                "/api/auth/google",
                headers=headers,
                body=body,
            )

            self.assertEqual(status, 302)
            location = headers["Location"]
            self.assertEqual(urlparse(location).path, "/login")
            self.assertEqual(
                parse_qs(urlparse(location).query)["auth_error"],
                [server.GOOGLE_AUTH_STATE_ERROR],
            )
            self.assertEqual(response_body, b"")

    def test_google_redirect_post_redirects_config_error_to_login(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)
            body = urlencode(
                {
                    "credential": "fake-token",
                    "state": server.sign_google_auth_state("/wanted"),
                }
            )
            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
            }

            with patch.object(
                server,
                "authenticate_google_credential",
                side_effect=server.AuthConfigError("google-auth dependencies are not installed"),
            ):
                status, headers, _ = self.request_test_server(
                    db_path,
                    "POST",
                    "/api/auth/google",
                    headers=headers,
                    body=body,
                )

            self.assertEqual(status, 302)
            location = headers["Location"]
            self.assertEqual(urlparse(location).path, "/login")
            params = parse_qs(urlparse(location).query)
            self.assertEqual(params["next"], ["/wanted"])
            self.assertEqual(
                params["auth_error"],
                ["Ошибка конфигурации деплоймента: google-auth dependencies are not installed"],
            )

    def test_user_state_is_scoped_per_google_user(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)

            user_one = self.create_google_user(db_path, "google-user-1", "one@example.com")
            user_two = self.create_google_user(db_path, "google-user-2", "two@example.com")
            anime_id = server.get_anime_list(db_path, user_id=user_one)[0]["id"]

            server.update_user_state(
                anime_id,
                {"is_favorite": True, "progress_episode_number": 3},
                db_path,
                user_one,
            )
            server.update_user_state(anime_id, {"watched": True}, db_path, user_two)

            detail_one = server.get_anime_detail(anime_id, db_path, user_one)
            detail_two = server.get_anime_detail(anime_id, db_path, user_two)
            self.assertTrue(detail_one["is_favorite"])
            self.assertEqual(detail_one["progress_episode_number"], 3)
            self.assertFalse(detail_one["watched"])
            self.assertFalse(detail_two["is_favorite"])
            self.assertIsNone(detail_two["progress_episode_number"])
            self.assertTrue(detail_two["watched"])

            rec_one = server.get_recommendations(db_path, user_id=user_one)
            rec_two = server.get_recommendations(db_path, user_id=user_two)
            self.assertEqual(rec_one["profile"]["mode"], "personalized")
            self.assertEqual(rec_two["profile"]["mode"], "personalized")
            self.assertEqual(rec_one["profile"]["favorite_count"], 1)
            self.assertEqual(rec_two["profile"]["favorite_count"], 0)

    def test_recommendations_are_ranked_and_exclude_known_titles(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)

            items = server.get_anime_list(db_path)
            favorite = next(item for item in items if item.get("genres") and item.get("source_count") > 0)
            server.update_user_state(favorite["id"], {"is_favorite": True}, db_path)

            payload = server.get_recommendations(db_path, limit=20)
            recommendations = payload["items"]
            self.assertGreater(len(recommendations), 0)
            self.assertLessEqual(len(recommendations), 20)
            self.assertTrue(all(item["source_count"] > 0 for item in recommendations))
            self.assertNotIn(favorite["id"], {item["id"] for item in recommendations})
            self.assertEqual(
                [item["recommendation_score"] for item in recommendations],
                sorted((item["recommendation_score"] for item in recommendations), reverse=True),
            )
            self.assertTrue(all(item["recommendation_reasons"] for item in recommendations))
            self.assertEqual(payload["profile"]["mode"], "personalized")

    def test_recommendations_prioritize_watchable_candidates(self):
        payload = server.get_recommendations(limit=server.MAX_RECOMMENDATION_LIMIT)
        recommendations = payload["items"]
        watchable_count = payload["profile"]["watchable_candidate_count"]
        if watchable_count:
            priority_count = min(watchable_count, len(recommendations))
            self.assertTrue(all(item["source_count"] > 0 for item in recommendations[:priority_count]))
            self.assertTrue(all(
                item["recommendation_components"]["watchable"] == 1.0
                for item in recommendations[:priority_count]
            ))
        self.assertEqual(
            [item["recommendation_score"] for item in recommendations],
            sorted((item["recommendation_score"] for item in recommendations), reverse=True),
        )

    def test_recommendations_have_popular_fallback_without_profile(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)
            con = server.connect(db_path)
            con.execute("delete from user_title_state")
            con.commit()
            con.close()

            payload = server.get_recommendations(db_path, limit=999)
            recommendations = payload["items"]
            self.assertEqual(payload["limit"], server.MAX_RECOMMENDATION_LIMIT)
            self.assertEqual(payload["profile"]["mode"], "popular")
            self.assertGreater(len(recommendations), 0)
            self.assertLessEqual(len(recommendations), server.MAX_RECOMMENDATION_LIMIT)
            self.assertTrue(all(item["source_count"] > 0 for item in recommendations))
            self.assertTrue(all("recommendation_components" in item for item in recommendations))

            fallback = server.get_recommendations(db_path, limit="not-a-number")
            self.assertEqual(fallback["limit"], server.DEFAULT_RECOMMENDATION_LIMIT)

    def test_player_markup_allows_fullscreen_and_picture_in_picture(self):
        html = Path(server.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="player"', html)
        self.assertIn('id="content-source"', html)
        self.assertIn("allowfullscreen", html)
        self.assertIn("fullscreen", html)
        self.assertIn("picture-in-picture", html)
        self.assertIn("web-share", html)
        self.assertIn("screen-wake-lock", html)
        self.assertIn('id="fullscreen-toggle"', html)
        self.assertIn('id="pip-toggle"', html)
        self.assertIn('id="recommendation-meta"', html)
        self.assertIn('href="/static/favicon.svg"', html)
        self.assertIn('href="/favicon.ico"', html)

    def test_login_uses_google_one_tap_and_button(self):
        html = Path(server.STATIC_DIR / "login.html").read_text(encoding="utf-8")
        js = Path(server.STATIC_DIR / "login.js").read_text(encoding="utf-8")
        self.assertIn("https://accounts.google.com/gsi/client?hl=ru", html)
        self.assertIn('id="one-tap-anchor"', html)
        self.assertIn('id="google-button"', html)
        self.assertIn("google.accounts.id.prompt", js)
        self.assertIn("function authError()", js)
        self.assertIn('get("auth_error")', js)
        self.assertIn("api/auth/config?next=", js)
        self.assertIn("auto_select: true", js)
        self.assertIn("callback: handleCredential", js)
        self.assertIn('fetch("/api/auth/google"', js)
        self.assertIn('credentials: "same-origin"', js)
        self.assertIn("complete_url", js)
        self.assertIn("authComplete()", js)
        self.assertIn("recoverExistingSession", js)
        self.assertIn("window.location.reload()", js)
        self.assertIn("sessionStorage.setItem(LOGIN_RECOVERY_STARTED_KEY", js)
        self.assertNotIn('ux_mode: "redirect"', js)
        self.assertNotIn("login_uri:", js)
        self.assertNotIn("use_fedcm_for_button", js)
        self.assertNotIn("button_auto_select", js)
        self.assertIn("google.accounts.id.renderButton", js)
        self.assertIn('const GOOGLE_LOCALE = "ru"', js)
        self.assertIn("locale: GOOGLE_LOCALE", js)
        self.assertIn("click_listener: handleGoogleButtonClick", js)
        self.assertIn("state: config.state", js)
        self.assertIn("maybeShowOneTap(google, Boolean(redirectError))", js)
        self.assertIn("renderUnavailableGoogleButton", js)
        self.assertIn("google-fallback-button", js)
        self.assertIn("Ошибка конфигурации деплоймента", js)

    def test_login_page_allows_google_popup_opener(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/animego.sqlite"
            shutil.copy(server.DEFAULT_DB, db_path)

            status, headers, body = self.request_test_server(db_path, "GET", "/login")

            self.assertEqual(status, 200)
            self.assertIn(b"accounts.google.com/gsi/client", body)
            self.assertEqual(headers["Cross-Origin-Opener-Policy"], "same-origin-allow-popups")
            self.assertEqual(headers["Referrer-Policy"], "no-referrer-when-downgrade")

    def test_view_mode_tabs_use_compact_accessible_labels(self):
        html = Path(server.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn('aria-label="Режим каталога"', html)
        self.assertIn('class="view-tab-icon"', html)
        self.assertIn('class="view-tab-label">Избр.</span>', html)
        self.assertIn('aria-label="Избранное"', html)
        self.assertIn('class="view-tab-label">Смотрю</span>', html)
        self.assertIn('aria-pressed="true"', html)

    def test_right_pane_deep_links_are_supported(self):
        js = Path(server.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn("readLinkState", js)
        self.assertIn("syncUrlFromDetail", js)
        self.assertIn("window.history", js)

    def test_right_pane_state_save_does_not_autoselect_first_filtered_item(self):
        js = Path(server.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        start = js.index("async function saveUserState")
        end = js.index("function applyFilter", start)
        save_user_state = js[start:end]

        self.assertIn("applyFilter();", save_user_state)
        self.assertNotIn("selectFirst", save_user_state)

    def test_yummyanime_mushoku_titles_are_available(self):
        items = server.get_anime_list()
        yummy = [item for item in items if item.get("source") == "yummyanime"]
        self.assertGreaterEqual(len(yummy), 4)
        self.assertTrue(any(item["title"] == "Реинкарнация безработного 3 сезон" for item in yummy))
        self.assertTrue(any(item["source_count"] > 0 for item in yummy))

        detail = server.get_anime_detail(next(item["id"] for item in yummy if item["source_count"] > 0))
        labels = {field["label"] for field in detail["fields"]}
        self.assertIn("Источник", labels)
        self.assertEqual(detail["source"], "yummyanime")

        season3 = server.get_anime_detail(next(item["id"] for item in yummy if item["title"] == "Реинкарнация безработного 3 сезон"))
        source_rows = [
            source
            for sources in season3["sources_by_episode"].values()
            for source in sources
        ]
        self.assertTrue(source_rows)
        self.assertEqual(source_rows[0]["provider_title"], "Kodik")
        self.assertFalse(any(source["provider_title"] == "Alloha" for source in source_rows))

    def test_duplicate_sources_are_exposed_as_canonical_titles(self):
        items = server.get_anime_list()
        merged = [
            item
            for item in items
            if {"animego", "yummyanime"}.issubset(set(item.get("sources") or []))
        ]
        self.assertGreaterEqual(len(merged), 10)

        item = merged[0]
        self.assertEqual(item["source"], "animego")
        self.assertEqual(item["source_variant_count"], 2)

        detail = server.get_anime_detail(item["id"])
        self.assertEqual(detail["id"], item["id"])
        self.assertEqual(detail["source"], "animego")
        self.assertIn("animego", detail["sources"])
        self.assertIn("yummyanime", detail["sources"])

        yummy_variant = next(variant for variant in detail["source_variants"] if variant["source"] == "yummyanime")
        same_detail = server.get_anime_detail(yummy_variant["id"])
        self.assertEqual(same_detail["id"], item["id"])
        self.assertEqual(same_detail["source"], "animego")

    def test_subtitle_matched_duplicate_sources_are_merged(self):
        items = server.get_anime_list()
        moon = [
            item
            for item in items
            if "Лунное путешествие приведёт к новому миру 2" in item["title"]
        ]

        self.assertEqual(len(moon), 1)
        self.assertEqual(moon[0]["source"], "animego")
        self.assertEqual(
            {variant["source"] for variant in moon[0]["source_variants"]},
            {"animego", "yummyanime"},
        )
        self.assertEqual(set(moon[0]["source_member_ids"]), {2463, 10001210})


if __name__ == "__main__":
    unittest.main()
