import unittest
import json
import subprocess
from unittest.mock import patch

import kodik_stream
from test_kodik_stream import FakeResponse, SerialSession, encode_url, serial_page


def player_page(value):
    return f'playerSettings.skipButton = parseSkipButton("{value}", "anime");'


class KodikIntroTests(unittest.TestCase):
    def test_provider_opening_ranges(self):
        cases = {
            "0-1:36,22:22-24:10": {"start": 0, "end": 96},
            "22:22-24:10, 0:30-2:00": {"start": 30, "end": 120},
            "[opening]1:05-2:35": {"start": 65, "end": 155},
            "[intro]0-1:02": {"start": 0, "end": 62},
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(kodik_stream.parse_kodik_intro(player_page(value)), expected)

    def test_no_opening_or_untrusted_metadata_is_ignored(self):
        pages = [
            "", player_page(""), player_page(",22:22-24:10"),
            player_page("22:22-24:10"), player_page("[ending]0-1:30"),
            player_page("[credits]0-1:30"), player_page("0-1:99"),
            player_page("10-10"), player_page("120-20"),
            player_page("0-9999999999999"), player_page("0-1:36;alert(1)"),
            player_page("0-1:36") + player_page("0-2:00"),
            'playerSettings.skipButton = parseSkipButton("0-1:36", "movie");',
        ]
        for page in pages:
            with self.subTest(page=page):
                self.assertIsNone(kodik_stream.parse_kodik_intro(page))

    def test_selected_translation_supplies_its_own_opening(self):
        class VoiceSession(SerialSession):
            def get(self, url, **kwargs):
                self.calls.append(("GET", url, kwargs))
                if "/serial/55/base/" in url:
                    page = serial_page("55", "base", "123", "hash", "963") + player_page("0-1:30")
                elif "/serial/77/other/" in url:
                    page = serial_page("77", "other", "456", "hash2", "17") + player_page("0:40-2:20")
                else:
                    return FakeResponse("unexpected", status=404)
                return FakeResponse(page)

        session = VoiceSession({"links": {"720": [{"src": encode_url("https://cloud2.solodcdn.com/video.m3u8")}]}})
        result = kodik_stream.resolve_kodik_player(
            "https://kodikplayer.com/serial/55/base/720p?season=1&episode=12",
            catalog_origin="https://catalog.test", translation_id="17", session=session,
        )
        self.assertEqual(result["translation_id"], "17")
        self.assertEqual(result["intro"], {"start": 40, "end": 140})

    def test_worker_discards_invalid_or_unexpected_metadata_without_breaking_video(self):
        for intro in ({"start": True, "end": 90}, {"start": 90, "end": 10}, {"start": 0, "end": 900}):
            payload = {
                "streams": {"720": "https://cloud2.solodcdn.com/video.m3u8"},
                "translations": [], "translation_id": None, "intro": intro, "extra": "discard",
            }
            result = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")
            with self.subTest(intro=intro), patch.object(kodik_stream.subprocess, "run", return_value=result):
                resolved = kodik_stream.resolve_kodik_streams_bounded(
                    "https://kodikplayer.com/seria/123/hash/720p", catalog_origin="https://catalog.test",
                )
                self.assertIsNone(resolved["intro"])
                self.assertNotIn("extra", resolved)


if __name__ == "__main__":
    unittest.main()
