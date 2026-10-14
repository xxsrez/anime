import json
import unittest
import uuid

import manual_episode_completion as manual
import server
import test_algorithm_correctness


class ManualCompletionTest(unittest.TestCase):
    def setUp(self):
        self.fixture = test_algorithm_correctness.PlaybackAlgorithmCorrectnessTest()
        self.fixture.setUp()
        self.db = self.fixture.db_path
        self.user = self.fixture.user_id
        self.fixture.seed_title(901, 'Manual test', episode_count=4)
        self.detail = server.get_anime_detail(901, self.db, self.user)

    def tearDown(self):
        self.fixture.tearDown()

    def payload(self, number):
        episode = next(e for e in self.detail['episodes'] if str(e['number']) == str(number))
        return dict(anime_id=901, episode_id=episode['id'], confirmed=True, operation_id=str(uuid.uuid4()))

    def call(self, payload):
        return manual.record(payload, self.db, self.user, server)

    def undo(self, result):
        return self.call(dict(action='undo', operation_id=result['operation_id']))

    def test_mark_only_one_and_undo_exactly(self):
        con = server.connect(self.db)
        before = manual.snapshot(con, self.user, [901])
        con.close()
        result = self.call(self.payload(2))
        detail = server.get_anime_detail(901, self.db, self.user)
        self.assertEqual(detail['watch_status'], 'watching')
        self.assertEqual(detail['progress_episode_number'], 2)
        self.assertEqual([e['number'] for e in detail['episodes'] if e['completed']], ['2'])
        row = self.fixture.episode_state(901, 2)
        self.assertEqual(row['engaged_seconds'], 0)
        self.assertEqual(row['last_event_type'], 'manual_completed')
        self.assertEqual(server.get_continue_watching(self.db, self.user)['item']['episode_number'], '3')
        self.undo(result)
        self.undo(result)
        con = server.connect(self.db)
        self.assertEqual(manual.snapshot(con, self.user, [901]), before)
        con.close()

    def test_idempotency_and_already_completed(self):
        payload = self.payload(2)
        result = self.call(payload)
        self.assertEqual(self.call(payload), result)
        self.assertTrue(self.call(self.payload(2))['already_completed'])
        self.undo(result)
        self.assertTrue(self.call(payload)['undone'])
        self.assertFalse(server.get_anime_detail(901, self.db, self.user)['episodes'][1]['completed'])

    def test_old_episode_keeps_later_progress(self):
        self.call(self.payload(3))
        self.call(self.payload(1))
        detail = server.get_anime_detail(901, self.db, self.user)
        self.assertEqual(detail['progress_episode_number'], 3)
        self.assertEqual(server.get_continue_watching(self.db, self.user)['item']['episode_number'], '4')

    def test_final_episode_finished_vs_ongoing(self):
        for status, expected in [('Онгоинг', 'watching'), ('Вышел', 'completed')]:
            con = server.connect(self.db)
            con.execute('update anime set status = ? where id = 901', (status,))
            con.commit()
            con.close()
            server.invalidate_catalog_cache(self.db)
            result = self.call(self.payload(4))
            self.assertEqual(server.get_anime_detail(901, self.db, self.user)['watch_status'], expected)
            self.undo(result)

    def test_undo_rejects_newer_progress(self):
        first = self.call(self.payload(1))
        self.call(self.payload(2))
        with self.assertRaisesRegex(ValueError, 'прогресс изменился'):
            self.undo(first)

    def test_validation_and_user_scope(self):
        payload = self.payload(1)
        payload['confirmed'] = False
        with self.assertRaises(ValueError):
            self.call(payload)
        payload['confirmed'] = True
        payload['episode_id'] = 9999999
        with self.assertRaises(ValueError):
            self.call(payload)
        result = self.call(self.payload(1))
        with self.assertRaises(ValueError):
            manual.record(dict(action='undo', operation_id=result['operation_id']), self.db, None, server)
        con = server.connect(self.db)
        row = con.execute('select after_state from user_manual_episode_completions').fetchone()
        self.assertEqual(json.loads(row[0])['event_id'], 0)
        con.close()

    def test_playback_after_mark_blocks_undo(self):
        result = self.call(self.payload(1))
        payload = self.fixture.watch_payload(self.detail, 2, 'AniDUB', 'later-playback')
        payload['event_type'] = 'player_engaged'
        server.record_watch_event(payload, self.db, self.user)
        with self.assertRaisesRegex(ValueError, 'прогресс изменился'):
            self.undo(result)

    def test_parallel_requests_create_one_completion(self):
        import concurrent.futures
        payloads = [self.payload(2), self.payload(2)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.call, payloads))
        self.assertEqual(sum(bool(result.get('already_completed')) for result in results), 1)
        con = server.connect(self.db)
        self.assertEqual(con.execute('select count(*) from user_manual_episode_completions').fetchone()[0], 1)
        con.close()


if __name__ == '__main__':
    unittest.main()
