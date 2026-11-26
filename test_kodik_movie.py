import unittest

import kodik_stream
from test_kodik_stream import FakeResponse, FakeSession, encode_url


MOVIE_URL = "https://kodikplayer.com/video/101/first/720p"
STREAM_URL = "https://cloud2.solodcdn.com/hls/test/movie.m3u8"


def movie_page(media_id="101", media_hash="first", selected="7", options=None):
    page = FakeSession({}).page.replace('vInfo.type = "seria"', 'vInfo.type = "video"')
    page = page.replace('vInfo.id = "123"', f'vInfo.id = "{media_id}"')
    page = page.replace('vInfo.hash = "hash"', f'vInfo.hash = "{media_hash}"')
    if options is None:
        options = (("7", "101", "first", "First"), ("8", "202", "second", "Second"))
    entries = []
    for voice, identity, digest, title in options:
        flag = "selected" if selected == voice else ""
        entries.append(
            f'<option data-id="{voice}" data-media-id="{identity}" data-media-hash="{digest}" '
            f'data-media-type="video" data-title="{title}" {flag}>{title}</option>'
        )
    return page + '<div class="movie-translations-box"><select>' + "".join(entries) + "</select></div>"


class MovieSession(FakeSession):
    def __init__(self, first=None, second=None, second_status=200):
        super().__init__({"links": {"720": [{"src": encode_url(STREAM_URL)}]}})
        self.first = movie_page() if first is None else first
        self.second = movie_page("202", "second", "8") if second is None else second
        self.second_status = second_status

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url == MOVIE_URL:
            return FakeResponse(self.first)
        if url == "https://kodikplayer.com/video/202/second/720p":
            return FakeResponse(self.second, status=self.second_status)
        return FakeResponse("unexpected", status=404)


class KodikMovieTests(unittest.TestCase):
    def resolve(self, session, translation=None):
        return kodik_stream.resolve_kodik_player(
            MOVIE_URL, catalog_origin="https://catalog.test", translation_id=translation, session=session,
        )

    def test_resolves_default_voice_and_exposes_film_translations(self):
        session = MovieSession()
        result = self.resolve(session)
        self.assertEqual(result["streams"], {"720": STREAM_URL})
        self.assertEqual(result["translation_id"], "7")
        self.assertEqual(result["translations"], [{"id": "7", "title": "First"}, {"id": "8", "title": "Second"}])
        self.assertEqual([call[0] for call in session.calls], ["GET", "POST"])
        self.assertEqual(session.calls[-1][2]["data"]["type"], "video")
        self.assertEqual(session.calls[-1][2]["data"]["id"], "101")

    def test_switches_to_the_verified_voice_of_the_same_film(self):
        session = MovieSession()
        result = self.resolve(session, "8")
        self.assertEqual(result["translation_id"], "8")
        self.assertEqual(result["streams"], {"720": STREAM_URL})
        self.assertEqual([call[0] for call in session.calls], ["GET", "GET", "POST"])
        self.assertEqual(session.calls[-1][2]["data"]["id"], "202")
        self.assertEqual(session.calls[-1][2]["headers"]["Referer"], session.calls[1][1])
        self.assertTrue(all(not call[2]["allow_redirects"] for call in session.calls))

    def test_a_film_without_a_translation_panel_still_plays(self):
        page = movie_page().split('<div class="movie-translations-box">')[0]
        result = self.resolve(MovieSession(first=page))
        self.assertEqual(result["streams"], {"720": STREAM_URL})
        self.assertEqual(result["translations"], [])
        self.assertIsNone(result["translation_id"])
        with self.assertRaises(kodik_stream.KodikTranslationUnavailable):
            self.resolve(MovieSession(first=page), "8")

    def test_rejects_invalid_or_ambiguous_film_identity(self):
        cases = (
            movie_page().replace('vInfo.id = "101"', 'vInfo.id = "999"'),
            movie_page().replace('vInfo.type = "video"', 'vInfo.type = "seria"'),
            movie_page(selected="8"),
            movie_page(selected=None),
            movie_page(options=(("7", "101", "first", "First"), ("7", "202", "second", "Duplicate"))),
        )
        for page in cases:
            session = MovieSession(first=page)
            with self.subTest(page=page), self.assertRaises(kodik_stream.KodikStreamError):
                self.resolve(session)
            self.assertFalse(any(call[0] == "POST" for call in session.calls))

    def test_switch_rejects_missing_redirected_or_unrelated_voices(self):
        unrelated = movie_page("202", "second", "8", options=(("8", "202", "second", "Second"),))
        wrong_media = movie_page("999", "second", "8")
        for session, voice in (
            (MovieSession(), "999"),
            (MovieSession(second_status=302), "8"),
            (MovieSession(second_status=404), "8"),
            (MovieSession(second=unrelated), "8"),
            (MovieSession(second=wrong_media), "8"),
        ):
            with self.subTest(voice=voice, status=session.second_status), self.assertRaises(kodik_stream.KodikTranslationUnavailable):
                self.resolve(session, voice)
            self.assertFalse(any(call[0] == "POST" for call in session.calls))

    def test_unsafe_translation_targets_never_leave_the_fixed_provider(self):
        page = movie_page(options=(("7", "101", "first", "First"), ("8", "202", "../evil", "Bad")))
        session = MovieSession(first=page)
        with self.assertRaises(kodik_stream.KodikTranslationUnavailable):
            self.resolve(session, "8")
        self.assertEqual([call[1] for call in session.calls], [MOVIE_URL])

    def test_rejects_unsafe_movie_urls_before_network_access(self):
        for url in (
            "https://kodikplayer.com.evil.test/video/101/first/720p",
            "https://kodikplayer.com@evil.test/video/101/first/720p",
            "http://kodikplayer.com/video/101/first/720p",
            "https://kodikplayer.com:8443/video/101/first/720p",
            "https://kodikplayer.com/video/101/first/720p#fragment",
        ):
            session = MovieSession()
            with self.subTest(url=url), self.assertRaises(kodik_stream.KodikStreamError):
                kodik_stream.resolve_kodik_player(url, catalog_origin="https://catalog.test", session=session)
            self.assertEqual(session.calls, [])


if __name__ == "__main__":
    unittest.main()
