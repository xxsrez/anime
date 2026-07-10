#!/usr/bin/env python3

import unittest
from unittest.mock import patch

import recommendation_model
import server


def item(item_id, title, *, genres=("Фэнтези",), score=8.0, **extra):
    payload = {
        "id": item_id,
        "title": title,
        "genres": list(genres),
        "effective_score": score,
        "aggregate_count": 1000,
        "source_count": 1,
        "available_episode_count": 1,
        "kind": "TV",
        "year": "2026",
        "status": "Завершён",
        "source": "animego",
    }
    payload.update(extra)
    return payload


class RecommendationV2IntegrationTest(unittest.TestCase):
    def test_server_uses_v2_and_excludes_every_known_or_negative_state(self):
        catalog = [
            item(1, "Favorite", is_favorite=True),
            item(2, "Completed", watch_status="completed"),
            item(3, "Planned", watch_status="planned"),
            item(4, "Dropped", watch_status="dropped"),
            item(5, "Dismissed", not_interested=True),
            item(6, "Candidate"),
        ]
        with patch("server.get_anime_list", return_value=catalog):
            payload = server.get_recommendations(limit=20, user_id=7)

        self.assertEqual(payload["model_version"], recommendation_model.MODEL_VERSION)
        self.assertEqual([entry["id"] for entry in payload["items"]], [6])
        self.assertIsNone(payload["items"][0]["recommendation_confidence"])
        self.assertTrue(payload["profile"]["confidence_available"])

    def test_server_filters_before_top_k(self):
        catalog = [
            item(index, f"Action {index}", genres=("Экшен",), score=9.5)
            for index in range(1, 30)
        ]
        catalog.append(item(100, "Drama", genres=("Драма",), score=5.5))

        with patch("server.get_anime_list", return_value=catalog):
            payload = server.get_recommendations(
                limit=1,
                user_id=7,
                filters={"genre": "Драма"},
            )

        self.assertEqual([entry["id"] for entry in payload["items"]], [100])
        self.assertEqual(payload["profile"]["filtered_candidate_count"], 1)

    def test_video_filter_values_are_explicit(self):
        self.assertEqual(server.normalize_recommendation_filters({"video": "with"}), {"video": True})
        self.assertEqual(server.normalize_recommendation_filters({"video": "missing"}), {"video": False})
        self.assertEqual(server.normalize_recommendation_filters({"video": "any"}), {})

    def test_watchable_count_respects_active_filters(self):
        catalog = [
            item(1, "Playable", source_count=1, available_episode_count=1),
            item(2, "Missing", source_count=0, available_episode_count=0),
        ]
        with patch("server.get_anime_list", return_value=catalog):
            payload = server.get_recommendations(
                limit=20,
                user_id=7,
                filters={"video": "missing"},
            )
        self.assertEqual(payload["profile"]["filtered_candidate_count"], 1)
        self.assertEqual(payload["profile"]["watchable_candidate_count"], 0)


if __name__ == "__main__":
    unittest.main()
