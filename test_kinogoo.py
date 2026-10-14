import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch, Mock

import kinogoo
import animego_scans
import server
import test_animego_scans as fixtures


class KinogooParserTest(unittest.TestCase):
    def test_multi_season_import_is_separate_and_idempotent(self):
        page = '<h1>Example 1-2 сезон (2022)</h1><iframe src="https://api.nextembed.ws/embed/kp/123"></iframe>'
        playlist = 'seasons:' + json.dumps([
            {'season': 1, 'episodes': [{'id': 41, 'episode': '1', 'hls': 'x'}]},
            {'season': 2, 'episodes': [{'id': 42, 'episode': '1', 'hls': 'x'}]},
        ])
        with tempfile.TemporaryDirectory() as tmp, patch('kinogoo.requests.get') as get:
            get.side_effect = lambda url, **kwargs: Mock(text=playlist if 'nextembed' in url else page)
            db = Path(tmp) / 'catalog.sqlite'
            first = kinogoo.import_title(db, 'https://kinogoo.fm/123-example.html', 1)
            second = kinogoo.import_title(db, 'https://kinogoo.fm/123-example.html', 2)
            self.assertEqual(first['id'], 30000123)
            self.assertNotEqual(first['id'], second['id'])
            kinogoo.import_title(db, 'https://kinogoo.fm/123-example.html', 2)
            con = server.connect(db)
            try:
                self.assertEqual(con.execute('select count(*) from anime').fetchone()[0], 2)
                self.assertEqual(con.execute('select count(*) from episodes').fetchone()[0], 2)
                self.assertIsNone(con.execute('select year from anime where id=?', (second['id'],)).fetchone()[0])
                task = animego_scans.task_payloads(con, [(second['id'], 'full')])[0]
                self.assertEqual(task['season'], 2)
                self.assertEqual(task['known_episode_ids'], [1500000042])
            finally:
                con.close()

    def test_invalid_season(self):
        for season in (0, 100, -1, True, '2'):
            with self.assertRaises(ValueError):
                kinogoo.title_identity(123, season)

    def test_playlist_not_page_counter(self):
        html = 'seasons:' + json.dumps([{'season': 1, 'episodes': [
            {'id': 10, 'episode': '1', 'hls': 'https://example.test/a'},
            {'id': 11, 'episode': '2'},
        ]}])
        result = kinogoo.parse_episodes(html, 'https://api.nextembed.ws/embed/kp/123?geoBlock=RU%2CEE', 1)
        self.assertEqual([r['episode']['number'] for r in result], ['1'])
        self.assertIn('geoBlock=RU%2CEE', result[0]['providers'][0]['embed_url'])
        self.assertEqual(result[0]['episode']['id'], 1500000010)
        with self.assertRaises(ValueError):
            kinogoo.parse_episodes(html, 'https://api.nextembed.ws/embed/kp/123', 2)

    def test_url_boundary(self):
        for url in ['http://kinogoo.fm/123-title.html', 'https://kinogoo.fm.evil/123-title.html',
                    'https://user@kinogoo.fm/123-title.html', 'https://kinogoo.fm/123-title.html?x=1']:
            with self.assertRaises(ValueError):
                kinogoo.page_id(url)

    def test_zero_numbered_extra_does_not_break_main_episodes(self):
        for zero in (0, "0"):
            episodes = [{"id": 10, "episode": zero, "hls": "x"},
                        {"id": 11, "episode": "1", "hls": "x"}]
            html = "seasons:" + json.dumps([{"season": 2, "episodes": episodes}])
            result = kinogoo.parse_episodes(html, "https://api.nextembed.ws/embed/kp/123", 2)
            self.assertEqual([x["episode"]["number"] for x in result], ["1"])
            with self.assertRaises(ValueError):
                kinogoo.parse_episodes("seasons:" + json.dumps([{"season": 2, "episodes": episodes[:1]}]),
                                      "https://api.nextembed.ws/embed/kp/123", 2)


class KinogooScanTest(unittest.TestCase):
    setUp = fixtures.AnimeGoScansTest.setUp
    tearDown = fixtures.AnimeGoScansTest.tearDown
    add_title = fixtures.AnimeGoScansTest.add_title
    create_job = fixtures.AnimeGoScansTest.create_job

    def test_scan_adds_and_replays_with_correct_source(self):
        self.add_title(30000123, source='kinogoo', status=None, episodes_text=None)
        job = self.create_job()
        self.assertEqual(job['tasks'][0]['source'], 'kinogoo')
        episodes = kinogoo.parse_episodes('seasons:[{"season":1,"episodes":[{"id":12,"episode":"1","hls":"x"}]}]',
                                         'https://api.nextembed.ws/embed/kp/123', 1)
        body = {'anime_id': 30000123, 'episodes': episodes, 'detail_html': '<h1>Wrong AnimeGO parser</h1>'}
        result = animego_scans.submit_scan_result(self.db_path, job['job']['id'], job['token'], body, server.PLAYER_HOSTS)
        self.assertEqual(result['result']['new_episode_count'], 1)
        result = animego_scans.submit_scan_result(self.db_path, job['job']['id'], job['token'], body, server.PLAYER_HOSTS)
        self.assertEqual(result['result']['status'], 'already_processed')
        con = server.connect(self.db_path)
        try:
            self.assertEqual(con.execute('select source from content_update_events').fetchone()[0], 'kinogoo')
            self.assertNotEqual(con.execute('select title from anime').fetchone()[0], 'Wrong AnimeGO parser')
        finally:
            con.close()
