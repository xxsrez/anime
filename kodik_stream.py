"""Resolve a catalogued Kodik episode to its native HLS streams.

The provider's response is untrusted. Only the fixed Kodik origin is contacted,
and only known CDN hosts are returned to the browser. Video is never proxied.
"""

import ast
import base64
import binascii
import json
import re
import string
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import requests
from bs4 import BeautifulSoup


KODIK_ORIGIN = "https://kodikplayer.com"
STREAM_CDN = "solodcdn.com"
MAX_RESPONSE_BYTES = 512 * 1024
MAX_URL_CHARS = 4096
REQUEST_TIMEOUT = (3, 10)
RESOLVE_DEADLINE_SECONDS = 20
WORKER_TRANSLATION_UNAVAILABLE = 75
USER_AGENT = "Mozilla/5.0 (compatible; AnimeCatalog/1.0)"
_ROT18 = str.maketrans(
    string.ascii_lowercase + string.ascii_uppercase,
    string.ascii_lowercase[18:] + string.ascii_lowercase[:18]
    + string.ascii_uppercase[18:] + string.ascii_uppercase[:18],
)


class KodikStreamError(ValueError):
    """A supported stream was not safely available."""


class KodikTranslationUnavailable(KodikStreamError):
    """The requested translation is not listed for this serial."""


def valid_kodik_episode_url(value):
    if not isinstance(value, str) or len(value) > MAX_URL_CHARS:
        return None
    value = "https:" + value if value.startswith("//") else value
    try:
        parsed = urlsplit(value)
        valid_host = parsed.hostname == "kodikplayer.com" and parsed.port in (None, 443)
    except ValueError:
        return None
    match = re.fullmatch(r"/(seria|serial)/(\d+)/([A-Za-z0-9_-]+)/(\d+p)/?", parsed.path)
    if (
        parsed.scheme != "https" or not valid_host or parsed.username or parsed.password
        or parsed.fragment or not match
    ):
        return None
    if match.group(1) == "serial":
        query = parse_qs(parsed.query, keep_blank_values=True)
        if any(
            len(query.get(key, [])) != 1 or not re.fullmatch(r"[1-9]\d{0,5}", query[key][0])
            for key in ("season", "episode")
        ):
            return None
    return value


def valid_stream_url(value):
    if not isinstance(value, str) or len(value) > MAX_URL_CHARS:
        return None
    value = "https:" + value if value.startswith("//") else value
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        safe_host = host == STREAM_CDN or host.endswith("." + STREAM_CDN)
        valid_port = parsed.port in (None, 443)
    except ValueError:
        return None
    if (
        parsed.scheme != "https" or not safe_host or not valid_port
        or parsed.username or parsed.password or parsed.fragment
        or not parsed.path.endswith(".m3u8")
    ):
        return None
    return value


def decode_stream_url(value):
    if not isinstance(value, str) or len(value) > MAX_URL_CHARS:
        return None
    if "//" not in value:
        encoded = value.translate(_ROT18)
        encoded += "=" * (-len(encoded) % 4)
        try:
            value = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return None
    return valid_stream_url(value)


def _js_string(page, name):
    pattern = rf"\b(?:var\s+)?{re.escape(name)}\s*=\s*([^;]{{1,8192}});"
    match = re.search(pattern, page)
    if not match:
        raise KodikStreamError("provider response is missing player data")
    try:
        value = ast.literal_eval(match.group(1).strip())
    except (ValueError, SyntaxError):
        raise KodikStreamError("provider returned invalid player data") from None
    if not isinstance(value, str) or len(value) > 4096:
        raise KodikStreamError("provider returned invalid player data")
    return value


def _read_response(response, deadline):
    if response.status_code != 200:
        raise KodikStreamError("provider returned an unsuccessful response")
    chunks = []
    size = 0
    for chunk in response.iter_content(chunk_size=16384):
        if time.monotonic() > deadline:
            raise KodikStreamError("provider response timed out")
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise KodikStreamError("provider response is too large")
        chunks.append(chunk)
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError:
        raise KodikStreamError("provider returned invalid text") from None


def _serial_page_info(page, embed_url, info):
    """Match the player page to the requested serial, season and episode."""
    parsed = urlsplit(embed_url)
    _, _, serial_id, serial_hash, _ = parsed.path.rstrip("/").split("/")
    query = parse_qs(parsed.query)
    season, episode = query["season"][0], query["episode"][0]
    serial_match = re.search(r"\b(?:var\s+)?serialId\s*=\s*Number\((\d+)\)\s*;", page)
    if not serial_match or serial_match.group(1) != serial_id or _js_string(page, "serialHash") != serial_hash:
        raise KodikStreamError("provider returned a different serial")
    soup = BeautifulSoup(page, "html.parser")
    seasons = [option for option in soup.select(".serial-seasons-box option")
               if option.get("value") == season and option.has_attr("selected")
               and option.get("data-serial-id") == serial_id
               and option.get("data-serial-hash") == serial_hash]
    episodes = [option for option in soup.select(f".serial-panel .season-{season} option")
                if option.get("value") == episode and option.get("data-id") == info["id"]
                and option.get("data-hash") == info["hash"]
                and option.get("data-other-translation") == "false"]
    if info["type"] != "seria" or len(seasons) != 1 or len(episodes) != 1:
        raise KodikStreamError("provider returned a different episode")
    episode_label = episodes[0].get("data-title") or episodes[0].get_text(" ", strip=True)
    visible_numbers = tuple(re.findall(r"\d+(?:[.,]\d+)?", episode_label))
    if not visible_numbers:
        raise KodikStreamError("provider returned an ambiguous episode")

    translations = {}
    selected = None
    for option in soup.select(".serial-translations-box option")[:100]:
        translation = option.get("data-id", "")
        media_id = option.get("data-media-id", "")
        media_hash = option.get("data-media-hash", "")
        title = option.get("data-title", "").strip()
        if (not re.fullmatch(r"[1-9]\d{0,9}", translation)
                or not re.fullmatch(r"[1-9]\d{0,19}", media_id)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", media_hash)
                or option.get("data-media-type") != "serial" or not title or len(title) > 160):
            continue
        if translation in translations:
            raise KodikStreamError("provider returned duplicate translations")
        translations[translation] = {"id": translation, "title": title, "media_id": media_id, "media_hash": media_hash}
        if option.has_attr("selected"):
            if selected is not None or (media_id, media_hash) != (serial_id, serial_hash):
                raise KodikStreamError("provider returned a different translation")
            selected = translation
    if not translations or selected is None:
        raise KodikStreamError("provider returned no supported translations")
    return translations, selected, visible_numbers


def resolve_kodik_player(embed_url, *, catalog_origin, translation_id=None, session=None):
    embed_url = valid_kodik_episode_url(embed_url)
    if not embed_url:
        raise KodikStreamError("unsupported provider URL")
    try:
        origin = urlsplit(catalog_origin)
        valid_origin = (
            origin.scheme in ("http", "https") and origin.hostname
            and not origin.username and not origin.password and not origin.path.strip("/")
            and not origin.query and not origin.fragment
        )
    except (TypeError, ValueError):
        valid_origin = False
    if not valid_origin:
        raise KodikStreamError("invalid catalog origin")
    client = session or requests.Session()
    deadline = time.monotonic() + RESOLVE_DEADLINE_SECONDS
    try:
        with client.get(
            embed_url,
            headers={"User-Agent": USER_AGENT, "Referer": catalog_origin.rstrip("/") + "/"},
            timeout=REQUEST_TIMEOUT, allow_redirects=False, stream=True,
        ) as response:
            page = _read_response(response, deadline)
        parsed = urlsplit(embed_url)
        _, media_type, media_id, media_hash, quality = parsed.path.rstrip("/").split("/")
        info = {name: _js_string(page, "vInfo." + name) for name in ("type", "hash", "id")}
        translations = {}
        selected = None
        if media_type == "serial":
            translations, selected, visible_numbers = _serial_page_info(page, embed_url, info)
            if translation_id and translation_id != selected:
                choice = translations.get(translation_id)
                if not choice:
                    raise KodikTranslationUnavailable("translation unavailable")
                query = parse_qs(parsed.query)
                query = urlencode({"season": query["season"][0], "episode": query["episode"][0]})
                selected_url = (f"{KODIK_ORIGIN}/serial/{choice['media_id']}/"
                                f"{choice['media_hash']}/{quality}?{query}")
                with client.get(
                    selected_url,
                    headers={"User-Agent": USER_AGENT, "Referer": catalog_origin.rstrip("/") + "/"},
                    timeout=REQUEST_TIMEOUT, allow_redirects=False, stream=True,
                ) as response:
                    if response.status_code == 404 or 300 <= response.status_code < 400:
                        raise KodikTranslationUnavailable("translation unavailable")
                    page = _read_response(response, deadline)
                try:
                    info = {name: _js_string(page, "vInfo." + name) for name in ("type", "hash", "id")}
                    _, actual_translation, selected_numbers = _serial_page_info(page, selected_url, info)
                except KodikStreamError:
                    raise KodikTranslationUnavailable("translation unavailable") from None
                if actual_translation != translation_id or selected_numbers != visible_numbers:
                    raise KodikTranslationUnavailable("translation unavailable")
                embed_url = selected_url
                selected = translation_id
        elif translation_id or (info["type"], info["id"], info["hash"]) != (media_type, media_id, media_hash):
            raise KodikStreamError("provider returned a different episode")

        data = {
            "d": _js_string(page, "domain"),
            "d_sign": _js_string(page, "d_sign"),
            "pd": _js_string(page, "pd"),
            "pd_sign": _js_string(page, "pd_sign"),
            "ref": _js_string(page, "ref"),
            "ref_sign": _js_string(page, "ref_sign"),
            "bad_user": "false",
            "cdn_is_working": "true",
        }
        data.update(info)
        if time.monotonic() > deadline:
            raise KodikStreamError("provider response timed out")
        with client.post(
            KODIK_ORIGIN + "/ftor", data=data,
            headers={
                "User-Agent": USER_AGENT, "Referer": embed_url,
                "Origin": KODIK_ORIGIN, "X-Requested-With": "XMLHttpRequest",
            },
            timeout=REQUEST_TIMEOUT, allow_redirects=False, stream=True,
        ) as response:
            payload = json.loads(_read_response(response, deadline))
    except (requests.RequestException, json.JSONDecodeError):
        raise KodikStreamError("provider could not return a stream") from None
    finally:
        if session is None:
            client.close()

    if not isinstance(payload, dict) or not isinstance(payload.get("links"), dict):
        raise KodikStreamError("provider returned no streams")
    streams = {}
    for quality, entries in payload["links"].items():
        if not isinstance(quality, str) or not re.fullmatch(r"\d{3,4}", quality):
            continue
        if not 144 <= int(quality) <= 4320 or not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and (url := decode_stream_url(entry.get("src"))):
                streams[quality] = url
                break
    if not streams:
        raise KodikStreamError("provider returned no supported streams")
    return {
        "streams": streams,
        "translations": [{"id": item["id"], "title": item["title"]} for item in translations.values()],
        "translation_id": selected,
    }


def resolve_kodik_streams(embed_url, *, catalog_origin, session=None):
    return resolve_kodik_player(embed_url, catalog_origin=catalog_origin, session=session)["streams"]


def resolve_kodik_streams_bounded(embed_url, *, catalog_origin, translation_id=None):
    """Run untrusted network I/O in a worker that can be stopped at the deadline."""
    if not valid_kodik_episode_url(embed_url):
        raise KodikStreamError("unsupported provider URL")
    if translation_id is not None and not re.fullmatch(r"[1-9]\d{0,9}", translation_id):
        raise KodikStreamError("unsupported translation")
    payload = json.dumps({"embed_url": embed_url, "catalog_origin": catalog_origin, "translation_id": translation_id})
    try:
        result = subprocess.run(
            [sys.executable, "-I", str(Path(__file__).resolve()), "--resolve"],
            input=payload, capture_output=True, text=True, timeout=RESOLVE_DEADLINE_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise KodikStreamError("provider could not return a stream") from None
    if result.returncode == WORKER_TRANSLATION_UNAVAILABLE and translation_id is not None:
        raise KodikTranslationUnavailable("translation unavailable")
    if result.returncode != 0 or len(result.stdout) > MAX_RESPONSE_BYTES:
        raise KodikStreamError("provider could not return a stream")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise KodikStreamError("provider could not return a stream") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("streams"), dict) or not payload["streams"] or not all(
        isinstance(quality, str) and re.fullmatch(r"\d{3,4}", quality)
        and 144 <= int(quality) <= 4320
        and valid_stream_url(url)
        for quality, url in payload["streams"].items()
    ):
        raise KodikStreamError("provider could not return a stream")
    translations, selected = payload.get("translations"), payload.get("translation_id")
    if not isinstance(translations, list) or len(translations) > 100 or any(
        not isinstance(item, dict) or set(item) != {"id", "title"}
        or not isinstance(item["id"], str) or not re.fullmatch(r"[1-9]\d{0,9}", item["id"])
        or not isinstance(item["title"], str) or not 0 < len(item["title"]) <= 160
        for item in translations
    ) or (selected is not None and selected not in {item["id"] for item in translations}):
        raise KodikStreamError("provider returned invalid translations")
    if (translations and selected is None) or (translation_id is not None and selected != translation_id):
        raise KodikStreamError("provider returned a different translation")
    return payload


def _worker_main():
    try:
        request = json.loads(sys.stdin.read(MAX_URL_CHARS * 2 + 1024))
        streams = resolve_kodik_player(
            request.get("embed_url"), catalog_origin=request.get("catalog_origin"),
            translation_id=request.get("translation_id"),
        )
    except KodikTranslationUnavailable:
        return WORKER_TRANSLATION_UNAVAILABLE
    except (KodikStreamError, ValueError, TypeError, AttributeError):
        return 1
    sys.stdout.write(json.dumps(streams, separators=(",", ":")))
    return 0


if __name__ == "__main__" and sys.argv[1:] == ["--resolve"]:
    raise SystemExit(_worker_main())
