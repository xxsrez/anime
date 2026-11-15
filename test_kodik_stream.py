import base64
import json
import string
import subprocess
import unittest
from unittest.mock import patch

import kodik_stream


def encode_url(value):
    encoded = base64.b64encode(value.encode()).decode().rstrip("=")
    letters = string.ascii_lowercase + string.ascii_uppercase
    rotated = (
        string.ascii_lowercase[8:] + string.ascii_lowercase[:8]
        + string.ascii_uppercase[8:] + string.ascii_uppercase[:8]
    )
    return encoded.translate(str.maketrans(letters, rotated))


class FakeResponse:
    def __init__(self, value, status=200):
        self.body = value.encode("utf-8")
        self.status_code = status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset:offset + chunk_size]


class FakeSession:
    def __init__(self, payload, page=None):
        self.calls = []
        self.page = page if page is not None else "\n".join([
            'var domain = "catalog.test";',
            'var d_sign = "signature";',
            'var pd = "kodikplayer.com";',
            'var pd_sign = "signature";',
            'var ref = "https%3A%2F%2Fcatalog.test%2F";',
            'var ref_sign = "signature";',
            'vInfo.type = "seria";',
            'vInfo.hash = "hash";',
            'vInfo.id = "123";',
        ])
        self.payload = payload

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return FakeResponse(self.page)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return FakeResponse(json.dumps(self.payload))


def serial_page(serial_id, serial_hash, episode_id, episode_hash, selected):
    common = FakeSession({}).page.replace('vInfo.id = "123"', f'vInfo.id = "{episode_id}"')
    common = common.replace('vInfo.hash = "hash"', f'vInfo.hash = "{episode_hash}"')
    return common + f'''
      <script>var serialId = Number({serial_id}); var serialHash = "{serial_hash}";</script>
      <div class="serial-panel">
        <div class="serial-seasons-box"><select><option selected value="1"
          data-serial-id="{serial_id}" data-serial-hash="{serial_hash}">1</option></select></div>
        <div class="season-1"><option value="12" data-id="{episode_id}"
          data-hash="{episode_hash}" data-other-translation="false">12</option></div>
        <div class="serial-translations-box"><select>
          <option data-id="963" data-media-id="55" data-media-hash="base"
            data-media-type="serial" data-title="First" {"selected" if selected == "963" else ""}>First</option>
          <option data-id="17" data-media-id="77" data-media-hash="other"
            data-media-type="serial" data-title="Second" {"selected" if selected == "17" else ""}>Second</option>
        </select></div>
      </div>'''


class SerialSession(FakeSession):
    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if "/serial/55/base/" in url:
            return FakeResponse(serial_page("55", "base", "123", "hash", "963"))
        if "/serial/77/other/" in url:
            return FakeResponse(serial_page("77", "other", "456", "hash2", "17"))
        return FakeResponse("unexpected", status=404)


class KodikStreamTests(unittest.TestCase):
    def test_resolves_only_valid_streams_without_advertising_data(self):
        expected = "https://cloud2.solodcdn.com/hls/token/episode.m3u8"
        session = FakeSession({
            "links": {
                "720": [{"src": encode_url(expected), "type": "application/x-mpegURL"}],
                "480": [{"src": "https://evil.example/video.m3u8"}],
            },
            "vast": [{"src": "https://ads.example/vast"}],
        })
        result = kodik_stream.resolve_kodik_streams(
            "//kodikplayer.com/seria/123/hash/720p?translations=false",
            catalog_origin="https://catalog.test", session=session,
        )
        self.assertEqual(result, {"720": expected})
        self.assertEqual([call[0] for call in session.calls], ["GET", "POST"])
        self.assertEqual(session.calls[1][1], "https://kodikplayer.com/ftor")
        self.assertTrue(all(not call[2]["allow_redirects"] for call in session.calls))
        self.assertTrue(all(call[2]["stream"] for call in session.calls))
        self.assertEqual(session.calls[1][2]["data"]["id"], "123")

    def test_rejects_non_kodik_episode_urls_before_network_access(self):
        for url in (
            "https://kodikplayer.com.evil.test/seria/123/hash/720p",
            "https://kodikplayer.com@evil.test/seria/123/hash/720p",
            "http://kodikplayer.com/seria/123/hash/720p",
            "https://kodikplayer.com:8443/seria/123/hash/720p",
            "https://kodikplayer.com/serial/123/hash/720p",
            "https://kodikplayer.com/serial/123/hash/720p?season=1&episode=1&episode=2",
            "https://kodikplayer.com/serial/123/hash/720p?season=1&episode=-1",
            "https://kodikplayer.com/seria/123/hash/720p#fragment",
        ):
            session = FakeSession({})
            with self.subTest(url=url), self.assertRaises(kodik_stream.KodikStreamError):
                kodik_stream.resolve_kodik_streams(url, catalog_origin="https://catalog.test", session=session)
            self.assertFalse(session.calls)

    def test_serial_resolves_requested_episode_and_selected_translation(self):
        expected = "https://cloud2.solodcdn.com/hls/token/episode.m3u8"
        session = SerialSession({"links": {"720": [{"src": encode_url(expected)}]}})
        result = kodik_stream.resolve_kodik_player(
            "https://kodikplayer.com/serial/55/base/720p?season=1&episode=12",
            catalog_origin="https://catalog.test", translation_id="17", session=session,
        )
        self.assertEqual(result["streams"], {"720": expected})
        self.assertEqual(result["translation_id"], "17")
        self.assertEqual(result["translations"], [{"id": "963", "title": "First"}, {"id": "17", "title": "Second"}])
        self.assertEqual([call[0] for call in session.calls], ["GET", "GET", "POST"])
        self.assertEqual(session.calls[2][2]["data"]["id"], "456")
        self.assertIn("season=1&episode=12", session.calls[1][1])

    def test_serial_rejects_mismatched_episode_or_unlisted_translation(self):
        for url, selected in (
            ("https://kodikplayer.com/serial/55/base/720p?season=1&episode=11", None),
            ("https://kodikplayer.com/serial/55/base/720p?season=1&episode=12", "999"),
        ):
            session = SerialSession({})
            with self.subTest(url=url), self.assertRaises(kodik_stream.KodikStreamError):
                kodik_stream.resolve_kodik_player(url, catalog_origin="https://catalog.test", translation_id=selected, session=session)
            self.assertFalse(any(call[0] == "POST" for call in session.calls))

    def test_serial_rejects_different_visible_episode_on_translation_switch(self):
        class WrongEpisodeSession(SerialSession):
            def get(self, url, **kwargs):
                response = super().get(url, **kwargs)
                if "/serial/77/other/" in url:
                    response.body = response.body.replace(b">12</option>", b">1-12</option>")
                return response

        session = WrongEpisodeSession({})
        with self.assertRaises(kodik_stream.KodikTranslationUnavailable):
            kodik_stream.resolve_kodik_player(
                "https://kodikplayer.com/serial/55/base/720p?season=1&episode=12",
                catalog_origin="https://catalog.test", translation_id="17", session=session,
            )
        self.assertFalse(any(call[0] == "POST" for call in session.calls))

    def test_missing_or_redirected_translation_does_not_hide_as_a_network_failure(self):
        class MissingTranslationSession(SerialSession):
            def __init__(self, status):
                super().__init__({})
                self.status = status

            def get(self, url, **kwargs):
                if "/serial/77/other/" in url:
                    self.calls.append(("GET", url, kwargs))
                    return FakeResponse("", status=self.status)
                return super().get(url, **kwargs)

        for status in (302, 404):
            session = MissingTranslationSession(status)
            with self.subTest(status=status), self.assertRaises(kodik_stream.KodikTranslationUnavailable):
                kodik_stream.resolve_kodik_player(
                    "https://kodikplayer.com/serial/55/base/720p?season=1&episode=12",
                    catalog_origin="https://catalog.test", translation_id="17", session=session,
                )
            self.assertFalse(any(call[0] == "POST" for call in session.calls))

    def test_rejects_unsafe_or_invalid_decoded_urls(self):
        for url in (
            "http://cloud2.solodcdn.com/episode.m3u8",
            "https://solodcdn.com.evil.test/episode.m3u8",
            "https://solodcdn.com@evil.test/episode.m3u8",
            "https://cloud2.solodcdn.com:8443/episode.m3u8",
            "https://cloud2.solodcdn.com/episode.mp4",
            "not-a-valid-base64-stream",
        ):
            with self.subTest(url=url):
                self.assertIsNone(kodik_stream.decode_stream_url(url))

    def test_rejects_unexpected_provider_response(self):
        cases = [
            FakeSession({}, page='var domain = "missing the rest";'),
            FakeSession({}, page=FakeSession({}).page.replace('vInfo.id = "123"', 'vInfo.id = "124"')),
            FakeSession({"links": {"720": [{"src": "https://evil.test/a.m3u8"}]}}),
            FakeSession({"links": []}),
            FakeSession({}, page="x" * (kodik_stream.MAX_RESPONSE_BYTES + 1)),
        ]
        for session in cases:
            with self.subTest(session=session), self.assertRaises(kodik_stream.KodikStreamError):
                kodik_stream.resolve_kodik_streams(
                    "https://kodikplayer.com/seria/123/hash/720p",
                    catalog_origin="https://catalog.test", session=session,
                )

    def test_abandons_a_response_after_the_deadline(self):
        session = FakeSession({})
        with patch.object(kodik_stream.time, "monotonic", side_effect=[100, 121]):
            with self.assertRaises(kodik_stream.KodikStreamError):
                kodik_stream.resolve_kodik_streams(
                    "https://kodikplayer.com/seria/123/hash/720p",
                    catalog_origin="https://catalog.test", session=session,
                )
        self.assertEqual(len(session.calls), 1)

    def test_worker_timeout_is_enforced_without_putting_url_in_arguments(self):
        secret_url = "https://kodikplayer.com/seria/123/sensitive-hash/720p"
        with patch.object(kodik_stream.subprocess, "run", side_effect=subprocess.TimeoutExpired("worker", 20)) as run:
            with self.assertRaises(kodik_stream.KodikStreamError):
                kodik_stream.resolve_kodik_streams_bounded(secret_url, catalog_origin="https://catalog.test")
        args, kwargs = run.call_args
        self.assertNotIn(secret_url, args[0])
        self.assertIn(secret_url, kwargs["input"])
        self.assertEqual(kwargs["timeout"], kodik_stream.RESOLVE_DEADLINE_SECONDS)

    def test_worker_output_is_validated_again(self):
        result = subprocess.CompletedProcess([], 0, stdout='{"streams":{"720":"https://evil.test/video.m3u8"},"translations":[],"translation_id":null}', stderr="")
        with patch.object(kodik_stream.subprocess, "run", return_value=result):
            with self.assertRaises(kodik_stream.KodikStreamError):
                kodik_stream.resolve_kodik_streams_bounded(
                    "https://kodikplayer.com/seria/123/hash/720p",
                    catalog_origin="https://catalog.test",
                )

    def test_worker_reports_unavailable_translation_separately(self):
        result = subprocess.CompletedProcess([], kodik_stream.WORKER_TRANSLATION_UNAVAILABLE, stdout="", stderr="")
        with patch.object(kodik_stream.subprocess, "run", return_value=result):
            with self.assertRaises(kodik_stream.KodikTranslationUnavailable):
                kodik_stream.resolve_kodik_streams_bounded(
                    "https://kodikplayer.com/serial/55/base/720p?season=1&episode=12",
                    catalog_origin="https://catalog.test", translation_id="17",
                )

    def test_python_usage_error_is_not_an_unavailable_translation(self):
        result = subprocess.CompletedProcess([], 2, stdout="", stderr="")
        with patch.object(kodik_stream.subprocess, "run", return_value=result):
            with self.assertRaises(kodik_stream.KodikStreamError) as error:
                kodik_stream.resolve_kodik_streams_bounded(
                    "https://kodikplayer.com/serial/55/base/720p?season=1&episode=12",
                    catalog_origin="https://catalog.test", translation_id="17",
                )
        self.assertNotIsInstance(error.exception, kodik_stream.KodikTranslationUnavailable)


if __name__ == "__main__":
    unittest.main()
