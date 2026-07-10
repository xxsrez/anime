import unittest

import user_state_model


class UserStateModelTest(unittest.TestCase):
    def test_infers_status_for_legacy_rows(self):
        self.assertEqual(
            user_state_model.normalized_state({"watched": 1})["watch_status"],
            "completed",
        )
        self.assertEqual(
            user_state_model.normalized_state({"progress_episode_number": 3})["watch_status"],
            "watching",
        )

    def test_planned_and_dropped_clear_active_progress(self):
        current = {"progress_episode_number": 4, "watch_status": "watching"}
        for status in ("planned", "dropped"):
            with self.subTest(status=status):
                result = user_state_model.apply_patch(current, {"watch_status": status}, "now")
                self.assertEqual(result["watch_status"], status)
                self.assertIsNone(result["progress_episode_number"])
                self.assertFalse(result["watched"])

    def test_completed_and_uncompleted_stay_backward_compatible(self):
        completed = user_state_model.apply_patch({}, {"watched": True}, "one")
        self.assertTrue(completed["watched"])
        self.assertEqual(completed["watch_status"], "completed")

        reopened = user_state_model.apply_patch(completed, {"watched": False}, "two")
        self.assertFalse(reopened["watched"])
        self.assertIsNone(reopened["watch_status"])

    def test_progress_promotes_to_watching_and_clear_removes_watching(self):
        watching = user_state_model.apply_patch({}, {"progress_episode_number": 2}, "one")
        self.assertEqual(watching["watch_status"], "watching")
        cleared = user_state_model.apply_patch(watching, {"progress_episode_number": None}, "two")
        self.assertIsNone(cleared["watch_status"])

    def test_not_interested_is_orthogonal_and_timestamped(self):
        result = user_state_model.apply_patch(
            {"is_favorite": True, "watch_status": "planned"},
            {"not_interested": True},
            "now",
        )
        self.assertTrue(result["is_favorite"])
        self.assertEqual(result["watch_status"], "planned")
        self.assertTrue(result["not_interested"])
        self.assertEqual(result["not_interested_updated_at"], "now")

    def test_timestamp_fields_only_move_for_owned_state(self):
        current = {
            "is_favorite": False,
            "watch_status": "planned",
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
