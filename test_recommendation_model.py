#!/usr/bin/env python3

from copy import deepcopy
import unittest

import recommendation_model as model


def catalog_item(
    item_id,
    title,
    *,
    genres=("Фэнтези",),
    rating=8.0,
    votes=1000,
    source_count=1,
    available_episode_count=12,
    kind="TV",
    year="2025",
    status="Завершён",
    source="animego",
    **extra,
):
    item = {
        "id": item_id,
        "title": title,
        "genres": list(genres),
        "effective_score": rating,
        "aggregate_count": votes,
        "source_count": source_count,
        "available_episode_count": available_episode_count,
        "kind": kind,
        "year": year,
        "status": status,
        "source": source,
    }
    item.update(extra)
    return item


class RecommendationModelTest(unittest.TestCase):
    def test_seed_weights_keep_explicit_favorite_dominant(self):
        favorite = catalog_item(1, "Favorite", is_favorite=True)
        watched = catalog_item(2, "Watched", watched=True)
        short_watch = catalog_item(3, "Short", meaningful_watch_seconds=300)
        long_watch = catalog_item(4, "Long", meaningful_watch_seconds=3600)
        very_long_watch = catalog_item(5, "Very long", meaningful_watch_seconds=100_000)

        self.assertEqual(model.seed_weight(favorite), 3.0)
        self.assertEqual(model.seed_weight(watched), model.WATCHED_SEED_WEIGHT)
        self.assertLess(model.seed_weight(watched), model.seed_weight(favorite) / 2)
        self.assertGreater(model.seed_weight(short_watch), 0.0)
        self.assertLess(model.seed_weight(short_watch), model.seed_weight(long_watch))
        self.assertLessEqual(
            model.seed_weight(very_long_watch),
            model.MEANINGFUL_WATCH_SEED_WEIGHT_CAP,
        )
        self.assertEqual(
            model.seed_weight(catalog_item(6, "Plan", is_favorite=True, watch_status="planned")),
            0.0,
        )

    def test_genre_aliases_and_idf_specificity_are_used(self):
        items = [
            catalog_item(
                1,
                "Seed",
                genres=("Фэнтези", "Исекай"),
                is_favorite=True,
            ),
            catalog_item(2, "Alias candidate", genres=("Исэкай",)),
        ]
        items.extend(
            catalog_item(index, f"Common {index}", genres=("Фэнтези",))
            for index in range(3, 13)
        )

        self.assertEqual(model.normalize_genre("Исекай"), model.normalize_genre("Исэкай"))
        payload = model.rank_recommendations(items, limit=20, diversity_weight=0)
        candidate = next(item for item in payload["items"] if item["id"] == 2)
        self.assertGreater(candidate["recommendation_components"]["genre"], 0)
        self.assertIn("Исэкай", candidate["recommendation_matched_genres"])

        top_genres = payload["profile"]["top_genres"]
        isekai = next(item for item in top_genres if item["genre"] == "Исэкай")
        fantasy = next(item for item in top_genres if item["genre"] == "Фэнтези")
        self.assertGreater(isekai["specificity"], fantasy["specificity"])
        self.assertGreater(isekai["weight"], fantasy["weight"])

    def test_nearest_seed_similarity_is_weighted_by_seed_strength(self):
        items = [
            catalog_item(1, "Favorite seed", genres=("Драма",), is_favorite=True),
            catalog_item(2, "Watched seed", genres=("Комедия",), watched=True),
            catalog_item(3, "Drama candidate", genres=("Драма",)),
            catalog_item(4, "Comedy candidate", genres=("Комедия",)),
        ]

        payload = model.rank_recommendations(
            items,
            limit=2,
            diversity_weight=0,
            franchise_cap=None,
        )
        by_id = {item["id"]: item for item in payload["items"]}
        self.assertGreater(
            by_id[3]["recommendation_components"]["neighbor"],
            by_id[4]["recommendation_components"]["neighbor"],
        )
        self.assertEqual(by_id[3]["recommendation_based_on"][0]["seed_weight"], 3.0)
        self.assertEqual(
            by_id[4]["recommendation_based_on"][0]["seed_weight"],
            model.WATCHED_SEED_WEIGHT,
        )

    def test_filters_and_predicates_run_before_top_k(self):
        items = [
            catalog_item(
                index,
                f"High action {index}",
                genres=("Экшен",),
                rating=10,
                votes=100_000,
            )
            for index in range(1, 10)
        ]
        items.extend(
            [
                catalog_item(
                    20,
                    "Wanted lower score",
                    genres=("Исекай",),
                    rating=6.5,
                    votes=10,
                    kind="Movie",
                    year="2024",
                    source="yummyanime",
                ),
                catalog_item(
                    21,
                    "Wrong source",
                    genres=("Исэкай",),
                    rating=9.5,
                    kind="Movie",
                    year="2024",
                    source="animego",
                ),
            ]
        )

        payload = model.rank_recommendations(
            items,
            limit=1,
            pool_size=1,
            filters={
                "genre": "Исэкай",
                "year_from": 2024,
                "year_to": 2024,
                "kind": "Movie",
                "status": "Завершён",
                "source": "yummyanime",
                "video": True,
            },
            predicates=lambda item: item["id"] == 20,
        )

        self.assertEqual([item["id"] for item in payload["items"]], [20])
        self.assertEqual(payload["profile"]["filtered_candidate_count"], 1)

    def test_franchise_cap_keeps_one_entry_per_known_franchise(self):
        items = [
            catalog_item(1, "Сага", subtitle="Saga", rating=10),
            catalog_item(2, "Сага 2", subtitle="Saga 2", rating=9.9),
            catalog_item(
                3,
                "Сага, сезон 3, часть 2",
                subtitle="Saga Season 3 Part 2",
                rating=9.8,
            ),
            catalog_item(6, "Сага OVA", subtitle="Saga OVA", rating=9.7),
            catalog_item(4, "Other A", rating=9.0),
            catalog_item(5, "Other B", rating=8.9),
        ]

        payload = model.rank_recommendations(
            items,
            limit=3,
            diversity_weight=0,
            franchise_cap=1,
        )
        result_ids = {item["id"] for item in payload["items"]}
        self.assertEqual(len(payload["items"]), 3)
        self.assertEqual(len(result_ids & {1, 2, 3, 6}), 1)

    def test_mmr_like_rerank_promotes_a_diverse_candidate(self):
        items = [
            catalog_item(1, "A strongest", genres=("Экшен", "Фэнтези"), rating=9.6),
            catalog_item(2, "B near duplicate", genres=("Экшен", "Фэнтези"), rating=9.5),
            catalog_item(3, "C diverse", genres=("Романтика", "Драма"), rating=9.3),
        ]

        payload = model.rank_recommendations(
            items,
            limit=3,
            diversity_weight=0.4,
            franchise_cap=None,
        )
        self.assertEqual([item["id"] for item in payload["items"][:2]], [1, 3])
        duplicate = next(item for item in payload["items"] if item["id"] == 2)
        self.assertGreater(
            duplicate["recommendation_components"]["diversity_penalty"],
            0,
        )

    def test_output_is_deterministic_and_does_not_mutate_inputs(self):
        items = [
            catalog_item(1, "Seed", genres=("Исекай", "Фэнтези"), is_favorite=True),
            catalog_item(2, "B", genres=("Исэкай",)),
            catalog_item(3, "A", genres=("Драма",)),
        ]
        original = deepcopy(items)

        first = model.rank_recommendations(items, limit=2)
        second = model.rank_recommendations(reversed(items), limit=2)

        self.assertEqual(first, second)
        self.assertEqual(items, original)

    def test_cold_start_is_explicit_and_has_no_fake_confidence(self):
        payload = model.rank_recommendations(
            [catalog_item(1, "A"), catalog_item(2, "B")],
            limit=2,
        )

        self.assertEqual(payload["profile"]["mode"], "cold_start")
        self.assertFalse(payload["profile"]["confidence_available"])
        self.assertEqual(payload["profile"]["seed_count"], 0)
        for item in payload["items"]:
            self.assertIsNone(item["recommendation_confidence"])
            self.assertIn("пока мало данных", item["recommendation_reasons"][0])

    def test_negative_and_library_states_are_known_but_not_positive_seeds(self):
        items = [
            catalog_item(1, "Planned", watch_status="planned"),
            catalog_item(2, "Dropped favorite", watch_status="dropped", is_favorite=True),
            catalog_item(3, "Not interested", not_interested=True),
            catalog_item(4, "Watching", watch_status="watching"),
            catalog_item(5, "Neutral"),
        ]

        payload = model.rank_recommendations(items, limit=10)
        self.assertEqual([item["id"] for item in payload["items"]], [5])
        self.assertEqual(payload["profile"]["mode"], "cold_start")
        self.assertEqual(payload["profile"]["seed_count"], 0)

    def test_long_series_gets_no_bonus_beyond_binary_playability(self):
        items = [
            catalog_item(
                1,
                "A short",
                rating=6.5,
                source_count=1,
                available_episode_count=1,
                effective_score_source="synthetic",
            ),
            catalog_item(
                2,
                "B long",
                rating=9.5,
                source_count=30,
                available_episode_count=300,
                effective_score_source="synthetic",
            ),
        ]

        payload = model.rank_recommendations(
            items,
            limit=2,
            diversity_weight=0,
            franchise_cap=None,
        )
        by_id = {item["id"]: item for item in payload["items"]}
        self.assertEqual(
            by_id[1]["recommendation_components"]["base_score"],
            by_id[2]["recommendation_components"]["base_score"],
        )
        self.assertEqual([item["id"] for item in payload["items"]], [1, 2])

    def test_scores_components_and_explanations_stay_in_public_ranges(self):
        items = [
            catalog_item(1, "Seed", genres=("Фэнтези", "Драма"), is_favorite=True),
            catalog_item(2, "Match", genres=("Фэнтези",), rating=9.1),
            catalog_item(3, "Different", genres=("Комедия",), rating=7.2),
        ]

        payload = model.rank_recommendations(items, limit=2)
        self.assertEqual(payload["model_version"], model.MODEL_VERSION)
        for item in payload["items"]:
            self.assertGreaterEqual(item["recommendation_score"], 0)
            self.assertLessEqual(item["recommendation_score"], 100)
            self.assertGreaterEqual(item["recommendation_base_score"], 0)
            self.assertLessEqual(item["recommendation_base_score"], 100)
            self.assertTrue(item["recommendation_reasons"])
            components = item["recommendation_components"]
            for key in (
                "genre",
                "neighbor",
                "kind",
                "quality",
                "playable",
                "popularity",
                "base_score",
                "rerank_score",
                "diversity_penalty",
            ):
                self.assertGreaterEqual(components[key], 0, key)
                self.assertLessEqual(components[key], 1, key)
            self.assertLessEqual(components["rerank_score"], components["base_score"])
            self.assertEqual(components["model_version"], model.MODEL_VERSION)


if __name__ == "__main__":
    unittest.main()
