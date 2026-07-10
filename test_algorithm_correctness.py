#!/usr/bin/env python3
import tempfile
import threading
import unittest

import scrape_animego
import server


class PlaybackAlgorithmCorrectnessTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = f"{self.tmpdir.name}/animego.sqlite"
        con = scrape_animego.init_db(self.db_path)
        con.close()
        server.prepare_database(self.db_path)
        self.user_id = self.create_user()

    def tearDown(self):
        server.invalidate_catalog_cache(self.db_path)
        server.reset_database_initialization(self.db_path)
        self.tmpdir.cleanup()

    def create_user(self):
        con = server.connect(self.db_path)
        try:
            user = server.upsert_google_user(
                con,
                {
                    "sub": "playback-correctness-user",
                    "email": "playback@example.test",
                    "email_verified": True,
                    "name": "Playback Test",
                    "picture": None,
                },
            )
            con.commit()
            return user["id"]
        finally:
            con.close()

    def seed_title(
        self,
        anime_id,
        title,
        *,
        subtitle=None,
        source="animego",
        episode_count=1,
        translations=("AniDUB", "Dream Cast"),
    ):
        con = server.connect(self.db_path)
        scraped_at = server.now_iso()
        try:
            con.execute(
                """
                insert into anime (
                    id, slug, title, subtitle, url, source, source_id, year, scraped_at
                ) values (?, ?, ?, ?, ?, ?, ?, '2026', ?)
                """,
                (
                    anime_id,
                    f"correctness-{anime_id}",
                    title,
                    subtitle,
                    f"https://example.test/anime/{anime_id}",
                    source,
                    str(anime_id),
                    scraped_at,
                ),
            )
            for episode_number in range(1, episode_count + 1):
                episode_id = anime_id * 100 + episode_number
                con.execute(
                    """
                    insert into episodes (id, anime_id, number, has_video, scraped_at)
                    values (?, ?, ?, 1, ?)
                    """,
                    (episode_id, anime_id, str(episode_number), scraped_at),
                )
                for translation_index, translation_title in enumerate(translations, start=1):
                    con.execute(
                        """
                        insert into video_sources (
                            anime_id, episode_id, provider_id, provider_title,
                            translation_id, translation_title, embed_host,
                            embed_url, embed_url_redacted, scraped_at
                        ) values (?, ?, ?, 'Kodik', ?, ?, 'kodikplayer.com', ?, ?, ?)
                        """,
                        (
                            anime_id,
                            episode_id,
                            f"kodik-{anime_id}-{episode_number}-{translation_index}",
                            str(translation_index),
                            translation_title,
                            f"https://kodikplayer.com/{anime_id}/{episode_number}/{translation_index}",
                            f"https://kodikplayer.com/{anime_id}/{episode_number}/<redacted>",
                            scraped_at,
                        ),
                    )
            con.commit()
        finally:
            con.close()
        server.invalidate_catalog_cache(self.db_path)
        return anime_id

    def source_for(self, detail, episode_number, translation_title):
        episode = next(
            episode
            for episode in detail["episodes"]
            if int(episode["number"]) == episode_number
        )
        source = next(
            source
            for source in detail["sources_by_episode"][episode["id"]]
            if source["translation_title"] == translation_title
        )
        return episode, source

    def watch_payload(self, detail, episode_number, translation_title, session_id):
        episode, source = self.source_for(detail, episode_number, translation_title)
        return {
            "client_session_id": session_id,
            "event_type": "episode_selected",
            "anime_id": detail["id"],
            "episode_id": episode["id"],
            "episode_number": episode["number"],
            "progress_episode_number": episode_number,
            "video_source_id": source["id"],
            "source": source["source"],
            "source_anime_id": source["source_anime_id"],
            "translation_id": source["translation_id"],
            "translation_title": source["translation_title"],
            "provider_id": source["provider_id"],
            "provider_title": source["provider_title"],
            "embed_host": source["embed_host"],
            "page_visible": True,
            "player_focused": True,
        }

    def episode_state(self, anime_id, episode_number):
        con = server.connect(self.db_path)
        try:
            row = con.execute(
                """
                select *
                from user_episode_state
                where user_id = ?
                  and anime_id = ?
                  and progress_episode_number = ?
                """,
                (self.user_id, anime_id, episode_number),
            ).fetchone()
            return dict(row) if row else None
        finally:
            con.close()

    def test_progress_patch_preserves_dream_cast_selected_by_watch_event(self):
        anime_id = self.seed_title(101, "Dubbing Race")
        detail = server.get_anime_detail(anime_id, self.db_path, self.user_id)
        _, dream_cast = self.source_for(detail, 1, "Dream Cast")
        self.assertEqual(
            detail["sources_by_episode"][detail["episodes"][0]["id"]][0]["translation_title"],
            "Dream Cast",
        )

        server.record_watch_event(
            self.watch_payload(detail, 1, "Dream Cast", "dream-cast-first"),
            self.db_path,
            self.user_id,
        )
        server.update_user_state(
            anime_id,
            {"progress_episode_number": 1},
            self.db_path,
            self.user_id,
        )

        state = self.episode_state(anime_id, 1)
        self.assertEqual(state["video_source_id"], dream_cast["id"])
        self.assertEqual(state["translation_title"], "Dream Cast")

    def test_manual_progress_without_watch_event_uses_smart_source_and_continue(self):
        anime_id = self.seed_title(102, "Manual Progress")

        saved = server.update_user_state(
            anime_id,
            {"progress_episode_number": 1},
            self.db_path,
            self.user_id,
        )

        state = self.episode_state(anime_id, 1)
        self.assertEqual(state["translation_title"], "Dream Cast")
        self.assertEqual(saved["last_watch"]["video_source_id"], state["video_source_id"])
        target = server.get_continue_watching(self.db_path, self.user_id)["item"]
        self.assertEqual(target["anime_id"], anime_id)
        self.assertEqual(target["video_source_id"], state["video_source_id"])
        self.assertEqual(target["reason"], "resume")

    def test_progress_patch_preserves_source_selected_on_canonical_variant_episode(self):
        primary_id = self.seed_title(
            104,
            "Variant Playback",
            subtitle="Variant Playback Romaji",
            translations=("Dream Cast",),
        )
        variant_id = self.seed_title(
            1000104,
            "Variant Playback",
            subtitle="Variant Playback Romaji",
            source="yummyanime",
            translations=("AniDUB",),
        )
        detail = server.get_anime_detail(primary_id, self.db_path, self.user_id)
        _, variant_source = self.source_for(detail, 1, "AniDUB")
        payload = self.watch_payload(detail, 1, "AniDUB", "variant-episode")
        payload["episode_id"] = variant_id * 100 + 1

        server.record_watch_event(payload, self.db_path, self.user_id)
        saved = server.update_user_state(
            primary_id,
            {"progress_episode_number": 1},
            self.db_path,
            self.user_id,
        )

        self.assertEqual(saved["last_watch"]["video_source_id"], variant_source["id"])
        target = server.get_continue_watching(self.db_path, self.user_id)["item"]
        self.assertEqual(target["video_source_id"], variant_source["id"])
        self.assertEqual(target["translation_id"], variant_source["translation_id"])

    def test_concurrent_manual_and_watch_updates_deterministically_keep_actual_source(self):
        anime_id = self.seed_title(103, "Concurrent Progress", episode_count=5)
        detail = server.get_anime_detail(anime_id, self.db_path, self.user_id)

        for episode_number in range(1, 6):
            with self.subTest(episode_number=episode_number):
                _, actual_source = self.source_for(detail, episode_number, "AniDUB")
                payload = self.watch_payload(
                    detail,
                    episode_number,
                    "AniDUB",
                    f"concurrent-{episode_number}",
                )
                barrier = threading.Barrier(3)
                errors = []

                def save_manual_progress(
                    barrier=barrier,
                    episode_number=episode_number,
                    errors=errors,
                ):
                    try:
                        barrier.wait(timeout=5)
                        server.update_user_state(
                            anime_id,
                            {"progress_episode_number": episode_number},
                            self.db_path,
                            self.user_id,
                        )
                    except Exception as exc:  # pragma: no cover - asserted below
                        errors.append(exc)

                def save_watch_event(barrier=barrier, payload=payload, errors=errors):
                    try:
                        barrier.wait(timeout=5)
                        server.record_watch_event(payload, self.db_path, self.user_id)
                    except Exception as exc:  # pragma: no cover - asserted below
                        errors.append(exc)

                threads = [
                    threading.Thread(target=save_manual_progress),
                    threading.Thread(target=save_watch_event),
                ]
                for thread in threads:
                    thread.start()
                barrier.wait(timeout=5)
                for thread in threads:
                    thread.join(timeout=10)

                self.assertFalse(any(thread.is_alive() for thread in threads))
                self.assertEqual(errors, [])
                state = self.episode_state(anime_id, episode_number)
                self.assertEqual(state["video_source_id"], actual_source["id"])
                self.assertEqual(state["translation_title"], "AniDUB")

    def test_watched_canonical_variant_is_skipped_by_continue(self):
        fallback_id = self.seed_title(201, "Still Watching", translations=("Dream Cast",))
        primary_id = self.seed_title(
            202,
            "Canonical Completed",
            subtitle="Canonical Completed Romaji",
            translations=("Dream Cast",),
        )
        variant_id = self.seed_title(
            1000202,
            "Canonical Completed",
            subtitle="Canonical Completed Romaji",
            source="yummyanime",
            translations=("Dream Cast",),
        )

        fallback_detail = server.get_anime_detail(fallback_id, self.db_path, self.user_id)
        server.record_watch_event(
            self.watch_payload(fallback_detail, 1, "Dream Cast", "fallback-title"),
            self.db_path,
            self.user_id,
        )
        completed_detail = server.get_anime_detail(primary_id, self.db_path, self.user_id)
        server.record_watch_event(
            self.watch_payload(completed_detail, 1, "Dream Cast", "completed-title"),
            self.db_path,
            self.user_id,
        )

        # Preserve the legacy/canonical-variant case explicitly: old data can
        # carry the watched flag on a non-primary source member.
        con = server.connect(self.db_path)
        try:
            con.execute(
                """
                insert into user_title_state (
                    user_id, anime_id, is_favorite, progress_episode_number, watched, updated_at
                ) values (?, ?, 0, 1, 1, ?)
                on conflict(user_id, anime_id) do update set
                    watched = 1,
                    updated_at = excluded.updated_at
                """,
                (self.user_id, variant_id, server.now_iso()),
            )
            con.commit()
        finally:
            con.close()

        target = server.get_continue_watching(self.db_path, self.user_id)["item"]
        self.assertEqual(target["anime_id"], fallback_id)
        self.assertNotEqual(target["anime_id"], completed_detail["id"])


if __name__ == "__main__":
    unittest.main()
