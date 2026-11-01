import json
import subprocess
import unittest

import server


class SearchInflectionsTest(unittest.TestCase):
    def test_query_limits_and_repeated_words_match_frontend(self):
        queries = [
            "Наруто Наруто",
            " ".join(f"слово{i}" for i in range(20)),
            "я" * 240 + " ещё",
            "🦊" * 200 + " Наруто и друзья" + "." * 100,
        ]
        expected = [server.search_query_info(query) for query in queries]
        result = subprocess.run(
            ["node", "-e", """
const search = require('./static/search.js');
const queries = JSON.parse(process.argv[1]);
const item = { id: 1, title: 'Наруто' };
const parsed = queries.map(query => search.searchQuery(query));
console.log(JSON.stringify({
  parsed,
  scores: parsed.map(query => search.scoreSearchItem(item, query)),
}));
""", json.dumps(queries)],
            cwd=server.ROOT, capture_output=True, text=True, check=True,
        )
        actual = json.loads(result.stdout)
        self.assertEqual(actual["parsed"], expected)
        item = {"id": 1, "title": "Наруто"}
        self.assertEqual(
            actual["scores"],
            [server.item_search_score(item, query) for query in expected],
        )

    def test_case_forms_and_frontend_backend_parity(self):
        items = [
            {"id": 1, "title": "Перевоплотился в седьмого принца"},
            {"id": 2, "title": "Перевоплотился в седьмого принца 2 сезон"},
            {"id": 3, "title": "Новый принц тенниса"},
            {"id": 4, "title": "Седьмой принц"},
            {"id": 5, "title": "Приключения храброго героя"},
        ]
        queries = ["Седьмой принц", "СЕДЬМОЙ ПРИНЦ", "седьмого принца",
                   "седьмому принцу", "храбрый герой", "седьмой дракон"]
        scores = [[server.item_search_score(item, server.search_query_info(q))
                   for item in items] for q in queries]
        for row in scores[:4]:
            self.assertGreater(row[0], 0)
            self.assertGreater(row[1], 0)
            self.assertEqual(row[2], 0)
        self.assertGreater(scores[0][3], scores[0][0])  # Exact title wins.
        self.assertGreater(scores[4][4], 0)  # Not a title-specific exception.
        self.assertEqual(scores[5], [0] * len(items))  # Both words still required.
        result = subprocess.run(
            ["node", "-e", """
const search = require('./static/search.js');
const {items, queries} = JSON.parse(process.argv[1]);
console.log(JSON.stringify(queries.map(q => items.map(item =>
  search.scoreSearchItem(item, search.searchQuery(q))))));
""", json.dumps({"items": items, "queries": queries})],
            cwd=server.ROOT, capture_output=True, text=True, check=True,
        )
        self.assertEqual(json.loads(result.stdout), scores)


if __name__ == "__main__":
    unittest.main()
