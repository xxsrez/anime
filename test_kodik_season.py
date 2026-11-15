import json
import subprocess
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import kodik_stream
from test_kodik_stream import FakeResponse, FakeSession, encode_url


SEASON_URL = (
    "https://kodikplayer.com/season/55/base/720p"
    "?translations=false&only_episode=true&only_season=true&episode=7"
)
STREAM_URL = "https://cloud2.solodcdn.com/hls/test/episode.m3u8"


def episode_page():
    page = FakeSession({}).page.replace('vInfo.id = "123"', 'vInfo.id = "456"')
    page = page.replace('vInfo.hash = "hash"', 'vInfo.hash = "episodehash"')
    return page + '\nplayerSettings.skipButton = parseSkipButton("0:45-2:16,21:20-22:51", "anime");'


def panel_page(option=None):
    if option is None:
        option = ('<option selected value="7" data-id="456" data-hash="episodehash" '
                  'data-title="7 серия" data-other-translation="false">7 серия</option>')
    return episode_page() + f'<div class="serial-panel"><div class="serial-series-box"><select>{option}</select></div></div>'


class SeasonSession(FakeSession):
    def __init__(self, panel=None, panel_status=200):
        super().__init__({"links": {"720": [{"src": encode_url(STREAM_URL)}]}})
        self.panel = panel if panel is not None else panel_page()
        self.panel_status = panel_status

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        options = parse_qs(urlsplit(url).query)
        if options.get("only_episode") == ["false"]:
            return FakeResponse(self.panel, status=self.panel_status)
        return FakeResponse(episode_page())


class KodikSeasonTests(unittest.TestCase):
    def test_resolves_exact_catalog_episode_and_its_opening(self):
        session = SeasonSession()
        result = kodik_stream.resolve_kodik_player(
            SEASON_URL, catalog_origin="https://catalog.test", expected_episode="7", session=session,
        )
        self.assertEqual(result["streams"], {"720": STREAM_URL})
        self.assertEqual(result["intro"], {"start": 45, "end": 136})
        self.assertEqual(result["translations"], [])
        self.assertEqual([call[0] for call in session.calls], ["GET", "GET", "POST"])
        self.assertEqual(session.calls[-1][2]["data"]["id"], "456")
        self.assertEqual(session.calls[-1][2]["headers"]["Referer"], SEASON_URL)
        self.assertEqual(parse_qs(urlsplit(session.calls[1][1]).query)["only_episode"], ["false"])
        self.assertTrue(all(not call[2]["allow_redirects"] for call in session.calls))
        self.assertEqual(kodik_stream.resolve_kodik_streams(
            SEASON_URL, catalog_origin="https://catalog.test", expected_episode="7", session=SeasonSession(),
        ), {"720": STREAM_URL})

    def test_rejects_unsafe_or_ambiguous_season_urls_without_network(self):
        bad_urls = (
            SEASON_URL + "&episode=8",
            SEASON_URL.replace("episode=7", "episode=-1"),
            SEASON_URL.replace("only_episode=true", "only_episode=false"),
            SEASON_URL.replace("translations=false", "translations=true"),
            SEASON_URL.replace("only_season=true", "only_season=false"),
            SEASON_URL.replace("&only_season=true", ""),
            SEASON_URL + "&only_episode=false",
            SEASON_URL + "&extra=unexpected",
            SEASON_URL.replace("kodikplayer.com", "kodikplayer.com.evil.test"),
            SEASON_URL + "#fragment",
        )
        for url in bad_urls:
            session = SeasonSession()
            with self.subTest(url=url), self.assertRaises(kodik_stream.KodikStreamError):
                kodik_stream.resolve_kodik_player(
                    url, catalog_origin="https://catalog.test", expected_episode="7", session=session,
                )
            self.assertEqual(session.calls, [])
        for expected in (None, "6", "7.5", "07", ""):
            session = SeasonSession()
            with self.subTest(expected=expected), self.assertRaises(kodik_stream.KodikStreamError):
                kodik_stream.resolve_kodik_player(
                    SEASON_URL, catalog_origin="https://catalog.test", expected_episode=expected, session=session,
                )
            self.assertEqual(session.calls, [])

    def test_refuses_a_different_or_ambiguous_selected_episode(self):
        good = ('<option selected value="7" data-id="456" data-hash="episodehash" '
                'data-title="7 серия" data-other-translation="false">7 серия</option>')
        options = (
            good.replace('value="7"', 'value="6"'),
            good.replace('data-id="456"', 'data-id="457"'),
            good.replace('data-hash="episodehash"', 'data-hash="otherhash"'),
            good.replace('data-other-translation="false"', 'data-other-translation="true"'),
            good.replace('data-title="7 серия"', 'data-title="6 серия"'),
            good.replace("selected ", ""),
            good + good,
        )
        for option in options:
            session = SeasonSession(panel_page(option))
            with self.subTest(option=option), self.assertRaises(kodik_stream.KodikStreamError):
                kodik_stream.resolve_kodik_player(
                    SEASON_URL, catalog_origin="https://catalog.test", expected_episode="7", session=session,
                )
            self.assertFalse(any(call[0] == "POST" for call in session.calls))

    def test_season_url_cannot_switch_to_a_different_voice(self):
        session = SeasonSession()
        with self.assertRaises(kodik_stream.KodikStreamError):
            kodik_stream.resolve_kodik_player(
                SEASON_URL, catalog_origin="https://catalog.test", expected_episode="7",
                translation_id="17", session=session,
            )
        self.assertEqual(session.calls, [])

    def test_rejects_mismatched_player_data_and_unavailable_panel(self):
        panels = [panel_page().replace('vInfo.id = "456"', 'vInfo.id = "457"'), panel_page()]
        for page, status in zip(panels, (200, 302), strict=True):
            session = SeasonSession(page, panel_status=status)
            with self.subTest(status=status), self.assertRaises(kodik_stream.KodikStreamError):
                kodik_stream.resolve_kodik_player(
                    SEASON_URL, catalog_origin="https://catalog.test", expected_episode="7", session=session,
                )
            self.assertFalse(any(call[0] == "POST" for call in session.calls))

    def test_worker_receives_catalog_episode_without_passing_url_in_arguments(self):
        output = json.dumps({"streams": {"720": STREAM_URL}, "translations": [], "translation_id": None})
        completed = subprocess.CompletedProcess([], 0, stdout=output, stderr="")
        with patch.object(kodik_stream.subprocess, "run", return_value=completed) as worker:
            kodik_stream.resolve_kodik_streams_bounded(
                SEASON_URL, catalog_origin="https://catalog.test", expected_episode="7",
            )
        args, kwargs = worker.call_args
        self.assertNotIn(SEASON_URL, args[0])
        self.assertEqual(json.loads(kwargs["input"])["expected_episode"], "7")


if __name__ == "__main__":
    unittest.main()
