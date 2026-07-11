import unittest

import user_state_model


class UserStateModelTest(unittest.TestCase):
    def test_default_and_legacy_rows_normalize_to_public_statuses(self):
        cases = (
            ({}, "none", None),
            ({"watched": 1}, "completed", None),
            ({"progress_episode_number": 3}, "watching", 3),
            ({"watch_status": "paused", "progress_episode_number": 4}, "watching", 4),
            ({"watch_status": "planned", "progress_episode_number": 5}, "none", None),
            ({"watch_status": "dropped", "progress_episode_number": 6}, "none", None),
            ({"watch_status": "unknown", "progress_episode_number": 7}, "none", None),
        )
        for row, expected_status, expected_progress in cases:
            with self.subTest(row=row):
                state = user_state_model.normalized_state(row)
                self.assertEqual(state["watch_status"], expected_status)
                self.assertEqual(state["progress_episode_number"], expected_progress)
                self.assertEqual(state["watched"], expected_status == "completed")

    def test_null_and_empty_status_patch_are_backward_compatible_none_aliases(self):
        current = {"progress_episode_number": 4, "watch_status": "watching"}
        for alias in (None, ""):
            with self.subTest(alias=alias):
                result = user_state_model.apply_patch(current, {"watch_status": alias}, "now")
                self.assertEqual(result["watch_status"], "none")
                self.assertIsNone(result["progress_episode_number"])
                self.assertFalse(result["watched"])

    def test_direct_legacy_status_patch_is_rejected(self):
        for status in ("planned", "paused", "dropped"):
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "watch_status"):
                user_state_model.validate_patch({"watch_status": status})

    def test_all_six_favorite_and_status_combinations_are_independent(self):
        for favorite in (False, True):
            for status in user_state_model.WATCH_STATUSES:
                with self.subTest(favorite=favorite, status=status):
                    result = user_state_model.apply_patch(
                        {},
                        {"is_favorite": favorite, "watch_status": status},
                        "now",
                    )
                    self.assertEqual(result["is_favorite"], favorite)
                    self.assertEqual(result["watch_status"], status)
                    self.assertEqual(result["watched"], status == "completed")
                    self.assertFalse(result["not_interested"])

    def test_completed_and_uncompleted_stay_backward_compatible(self):
        completed = user_state_model.apply_patch({}, {"watched": True}, "one")
        self.assertTrue(completed["watched"])
        self.assertEqual(completed["watch_status"], "completed")

        reopened = user_state_model.apply_patch(completed, {"watched": False}, "two")
        self.assertFalse(reopened["watched"])
        self.assertEqual(reopened["watch_status"], "none")

    def test_progress_promotes_to_watching_and_clear_returns_to_none(self):
        watching = user_state_model.apply_patch({}, {"progress_episode_number": 2}, "one")
        self.assertEqual(watching["watch_status"], "watching")
        cleared = user_state_model.apply_patch(watching, {"progress_episode_number": None}, "two")
        self.assertEqual(cleared["watch_status"], "none")

    def test_favorite_and_negative_feedback_cannot_contradict(self):
        negative = user_state_model.apply_patch({}, {"not_interested": True}, "one")
        self.assertTrue(negative["not_interested"])

        favorite = user_state_model.apply_patch(negative, {"is_favorite": True}, "two")
        self.assertTrue(favorite["is_favorite"])
        self.assertFalse(favorite["not_interested"])
        self.assertEqual(favorite["favorite_updated_at"], "two")
        self.assertEqual(favorite["not_interested_updated_at"], "two")

        negative_again = user_state_model.apply_patch(
            favorite,
            {"not_interested": True},
            "three",
        )
        self.assertFalse(negative_again["is_favorite"])
        self.assertTrue(negative_again["not_interested"])
        self.assertEqual(negative_again["favorite_updated_at"], "three")
        self.assertEqual(negative_again["not_interested_updated_at"], "three")

        stale = user_state_model.normalized_state(
            {"is_favorite": True, "not_interested": True}
        )
        self.assertTrue(stale["is_favorite"])
        self.assertFalse(stale["not_interested"])

        with self.assertRaisesRegex(ValueError, "favorite titles"):
            user_state_model.validate_patch(
                {"is_favorite": True, "not_interested": True}
            )

    def test_timestamp_fields_only_move_for_owned_state(self):
        current = {
            "is_favorite": False,
            "watch_status": "none",
            "favorite_updated_at": "old-favorite",
            "watch_status_updated_at": "old-status",
        }
        favorite = user_state_model.apply_patch(current, {"is_favorite": True}, "new")
        self.assertEqual(favorite["favorite_updated_at"], "new")
        self.assertEqual(favorite["watch_status_updated_at"], "old-status")

    def test_validation_is_strict(self):
        invalid = (
            ({}, "state patch must contain"),
            ({"watched": 1}, "watched must be"),
            ({"progress_episode_number": -1}, "progress_episode_number"),
            ({"watch_status": "later"}, "watch_status"),
            ({"unknown": True}, "unsupported state field"),
        )
        for patch, message in invalid:
            with self.subTest(patch=patch), self.assertRaisesRegex(ValueError, message):
                user_state_model.validate_patch(patch)


if __name__ == "__main__":
    unittest.main()
