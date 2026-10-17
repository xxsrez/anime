import unittest

import server


class TitleSlugTests(unittest.TestCase):
    def test_unique_titles_have_clean_transliterated_slugs(self):
        groups = [{"id": 2727, "title": "Монолог фармацевта 2"},
                  {"id": 3279, "title": "Re:Zero — Жизнь в альтернативном мире с нуля 4"}]
        server.assign_catalog_slugs(groups)
        self.assertEqual(groups[0]["slug"], "monolog-farmacevta-2")
        self.assertEqual(groups[1]["slug"], "re-zero-zhizn-v-alternativnom-mire-s-nulya-4")

    def test_collisions_are_deterministic_and_keep_one_clean_address(self):
        groups = [{"id": 2, "title": "Same!"}, {"id": 1, "title": "Same"}]
        server.assign_catalog_slugs(groups)
        self.assertEqual(groups[0]["slug"], "same-2")
        self.assertEqual(groups[1]["slug"], "same")
        server.assign_catalog_slugs(list(reversed(groups)))
        self.assertEqual(groups[0]["slug"], "same-2")

    def test_clean_names_never_steal_legacy_variant_or_numeric_routes(self):
        groups = [{"id": 1, "title": "Different", "source_variants": [{"id": 36, "title": "Original"}]},
                  {"id": 2, "title": "Original 10"},
                  {"id": 3, "title": "86"}, {"id": 4, "title": "Admin"}]
        server.assign_catalog_slugs(groups)
        self.assertEqual([item["slug"] for item in groups], ["different", "original-10-2", "86-3", "admin-4"])

    def test_truncation_collisions_get_a_suffix(self):
        groups = [{"id": 1, "title": "x" * 80 + "a"}, {"id": 2, "title": "x" * 80 + "b"}]
        server.assign_catalog_slugs(groups)
        self.assertNotEqual(groups[0]["slug"], groups[1]["slug"])


if __name__ == "__main__":
    unittest.main()
