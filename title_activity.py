"""Track real catalog additions and observed update dates from source sites."""

from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import gzip
import io
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlparse
from urllib.request import Request
import xml.etree.ElementTree as ET

from scripts.http_safety import open_validated_url


SCHEMA_FILE = (
    Path(__file__).resolve().parent
    / "migrations/2026-11-15_title-content-activity/00_create_title_content_activity.sql"
)
SITEMAP_NAMESPACE = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
ANIMEGO_SITEMAP = "https://animego.me/sitemap.anime.xml"
YUMMY_SITEMAP = "https://ru.yummyani.me/sitemapcatalog.xml.gz"
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024
MAX_XML_BYTES = 8 * 1024 * 1024
DATE_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")


class SourceDateRefreshError(RuntimeError):
    """A required source snapshot could not be refreshed safely."""


def ensure_schema(con):
    """Install the same idempotent schema used by the tracked migration."""
    statement = []
    for char in SCHEMA_FILE.read_text(encoding="utf-8"):
        statement.append(char)
        if char == ";" and sqlite3.complete_statement("".join(statement)):
            con.execute("".join(statement))
            statement.clear()
    if "".join(statement).strip():
        raise ValueError("incomplete title activity schema")


def normalize_source_date(value, observed_at):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("source update date is missing")
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid source update date") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    observed_at = observed_at if observed_at.tzinfo else observed_at.replace(tzinfo=dt.timezone.utc)
    if parsed > observed_at + dt.timedelta(days=1):
        raise ValueError("source update date is in the future")
    return parsed.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def newest_video_date(videos, observed_at):
    dates = []
    for video in videos:
        value = video.get("date") if isinstance(video, dict) else None
        if type(value) is not int or value <= 0:
            continue
        try:
            date = dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat(timespec="seconds")
            dates.append(normalize_source_date(date, observed_at))
        except (ValueError, OverflowError, OSError):
            continue
    return max(dates, default=None)


def record_detail_date(con, item, detail, observed_at):
    value = detail.get("source_updated_at")
    if item.get("source") != "yummyanime" or not value:
        return
    if not con.execute(
        "select 1 from anime where id=? and source='yummyanime' and source_id=?",
        (item["id"], str(item.get("source_id") or "")),
    ).fetchone():
        return
    if isinstance(observed_at, str):
        observed_at = dt.datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    record_source_date(con, item["id"], value, observed_at, source_kind="yummyanime_video")


def record_source_date(con, anime_id, value, observed_at, *, source_kind=None):
    value = normalize_source_date(value, observed_at)
    checked = observed_at if observed_at.tzinfo else observed_at.replace(tzinfo=dt.timezone.utc)
    checked = checked.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    con.execute(
        """
        insert into title_content_activity(anime_id, source_updated_at, source_checked_at, source_kind)
        values (?, ?, ?, ?)
        on conflict(anime_id) do update set
            source_updated_at = case
                when julianday(excluded.source_updated_at) > coalesce(julianday(source_updated_at), -1)
                then excluded.source_updated_at else source_updated_at end,
            source_checked_at = case
                when julianday(excluded.source_checked_at) > coalesce(julianday(source_checked_at), -1)
                then excluded.source_checked_at else source_checked_at end,
            source_kind = case
                when julianday(excluded.source_updated_at) > coalesce(julianday(source_updated_at), -1)
                then excluded.source_kind else source_kind end
        """,
        (int(anime_id), value, checked, source_kind),
    )


def _source_path(value, hostname, prefix):
    try:
        parsed = urlparse(value)
        if (parsed.scheme != "https" or parsed.hostname != hostname or parsed.port is not None
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or not parsed.path.startswith(prefix)):
            return None
    except ValueError:
        return None
    tail = parsed.path[len(prefix):].rstrip("/")
    return tail if tail and "/" not in tail else None


def _parse_sitemap(data, observed_at, source):
    if (len(data) > MAX_XML_BYTES or b"\x00" in data
            or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper()):
        raise ValueError("invalid sitemap")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError("invalid sitemap") from exc
    if root.tag != SITEMAP_NAMESPACE + "urlset":
        raise ValueError("invalid sitemap root")
    result = {}
    for entry in root.findall(SITEMAP_NAMESPACE + "url"):
        location = entry.findtext(SITEMAP_NAMESPACE + "loc") or ""
        if source == "animego":
            slug = _source_path(location, "animego.me", "/anime/")
            match = re.search(r"-(\d+)$", slug or "")
            key = int(match.group(1)) if match else None
        else:
            key = _source_path(location, "ru.yummyani.me", "/catalog/item/")
        if key is None:
            continue
        try:
            value = normalize_source_date(entry.findtext(SITEMAP_NAMESPACE + "lastmod"), observed_at)
        except ValueError:
            continue
        # Duplicate entries must not make a date move backwards.
        if key not in result or value > result[key]:
            result[key] = value
    return result


def parse_animego_sitemap(data, observed_at):
    return _parse_sitemap(data, observed_at, "animego")


def parse_yummy_sitemap(data, observed_at):
    return _parse_sitemap(data, observed_at, "yummyanime")


def _fetch_sitemap(url, hosts, compressed, parser, observed_at):
    request = Request(url, headers={"User-Agent": "AnimeLocal/1.0", "Accept": "application/xml,*/*"})
    with open_validated_url(request, timeout=12, allowed_hosts=hosts) as response:
        data = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError("sitemap download is too large")
    if compressed:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            data = stream.read(MAX_XML_BYTES + 1)
    result = parser(data, observed_at)
    if not result:
        raise ValueError("sitemap contains no valid update dates")
    return result


def fetch_source_snapshots(observed_at=None):
    """Fetch both snapshots before opening a database write transaction."""
    observed_at = observed_at or dt.datetime.now(dt.timezone.utc)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            animego = pool.submit(_fetch_sitemap, ANIMEGO_SITEMAP, ("animego.me",), False,
                                  parse_animego_sitemap, observed_at)
            yummy = pool.submit(_fetch_sitemap, YUMMY_SITEMAP,
                                ("ru.yummyani.me", "static.yani.tv"), True,
                                parse_yummy_sitemap, observed_at)
            return {"animego": animego.result(), "yummyanime": yummy.result()}
    except Exception as exc:
        raise SourceDateRefreshError("Не удалось обновить даты источников. Повторите позже.") from exc


def apply_source_snapshots(con, snapshots, observed_at):
    """Update known rows only; ambiguous YummyAni slug matches are ignored."""
    animego_dates = snapshots.get("animego", {})
    yummy_dates = snapshots.get("yummyanime", {})
    rows = con.execute("select id, source, url from anime where source in ('animego', 'yummyanime')").fetchall()
    yummy_rows = []
    for row in rows:
        anime_id, source, url = row[0], row[1], row[2]
        if source == "animego" and anime_id in animego_dates:
            record_source_date(con, anime_id, animego_dates[anime_id], observed_at, source_kind="animego_sitemap")
        elif source == "yummyanime":
            slug = _source_path(url or "", "ru.yummyani.me", "/catalog/item/")
            if slug:
                yummy_rows.append((anime_id, slug))

    source_by_base = {}
    for slug in yummy_dates:
        source_by_base.setdefault(DATE_SUFFIX.sub("", slug), []).append(slug)
    db_by_base = {}
    for anime_id, slug in yummy_rows:
        db_by_base.setdefault(DATE_SUFFIX.sub("", slug), []).append(anime_id)
    count = sum(1 for row in rows if row[1] == "animego" and row[0] in animego_dates)
    for anime_id, slug in yummy_rows:
        source_slug = slug if slug in yummy_dates else None
        if source_slug is None:
            base = DATE_SUFFIX.sub("", slug)
            candidates = source_by_base.get(base, [])
            if len(candidates) == 1 and len(db_by_base[base]) == 1:
                source_slug = candidates[0]
        if source_slug is not None:
            record_source_date(con, anime_id, yummy_dates[source_slug], observed_at, source_kind="yummyanime_sitemap")
            count += 1
    return count
