#!/usr/bin/env python3
import argparse
import datetime as dt
import html
import json
import re
import sqlite3
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode
from urllib.request import Request

from bs4 import BeautifulSoup
from scripts.http_safety import open_validated_url
from scripts.operation_lock import DatabaseOperationLock, default_lock_path


BASE_URL = "https://animego.me"
START_URL = f"{BASE_URL}/anime/status/ongoing"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def clean_text(value):
    if value is None:
        return None
    text = " ".join(value.get_text(" ", strip=True).split()) if hasattr(value, "get_text") else str(value)
    return html.unescape(text).strip() or None


def parse_score(value):
    if value is None:
        return None
    text = clean_text(value) if hasattr(value, "get_text") else str(value)
    if not text:
        return None
    match = re.search(r"\d+(?:[,.]\d+)?", text)
    return float(match.group(0).replace(",", ".")) if match else None


def parse_int(value):
    if value is None:
        return None
    text = clean_text(value) if hasattr(value, "get_text") else str(value)
    if not text:
        return None
    match = re.search(r"\d+", text.replace("\xa0", " "))
    return int(match.group(0)) if match else None


def absolute_url(url):
    return urljoin(BASE_URL, url) if url else None


def fetch_text(url, ajax=False, referer=None, delay=0.0):
    if delay:
        time.sleep(delay)
    headers = {"User-Agent": USER_AGENT}
    if ajax:
        headers["X-Requested-With"] = "XMLHttpRequest"
    if referer:
        headers["Referer"] = referer
    target = absolute_url(url)
    req = Request(target, headers=headers)
    with open_validated_url(req, timeout=30, allowed_hosts=("animego.me",)) as response:
        return response.read().decode("utf-8", "replace")


def parse_anime_id(url):
    path = urlparse(url).path.rstrip("/")
    match = re.search(r"-(\d+)$", path)
    return int(match.group(1)) if match else None


def parse_slug(url):
    path = urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1]


def parse_listing(html_text):
    soup = BeautifulSoup(html_text, "lxml")
    items = []
    for item in soup.select(".ani-list__item"):
        title_link = item.select_one(".ani-list__item-title a[href^='/anime/']")
        if not title_link:
            continue
        url = absolute_url(title_link.get("href"))
        anime_id = parse_anime_id(url)
        if not anime_id:
            continue
        cover = item.select_one("img.image__img")
        rating_badge = item.select_one(".rating-badge")
        tags = []
        for link in item.select(".ani-list__item-genres__link"):
            href = link.get("href") or ""
            text = clean_text(link)
            if text:
                tags.append({"text": text, "href": href})
        item_type = next((t["text"] for t in tags if "/anime/type/" in t["href"]), None)
        year = next((t["text"] for t in tags if re.match(r"^/anime/season/\d{4}$", t["href"])), None)
        genres = [t["text"] for t in tags if "/anime/genre/" in t["href"]]
        items.append(
            {
                "id": anime_id,
                "slug": parse_slug(url),
                "title": title_link.get("title") or clean_text(title_link),
                "subtitle": clean_text(item.select_one(".fw-lighter.small")),
                "url": url,
                "cover_url": absolute_url(cover.get("src")) if cover else None,
                "listing_score": parse_score(rating_badge),
                "kind": item_type,
                "year": year,
                "genres": genres,
                "listing_description": clean_text(item.select_one(".ani-list__item-description")),
            }
        )
    return items


def parse_detail(html_text):
    soup = BeautifulSoup(html_text, "lxml")
    fields = {}
    schema_data = {}
    current_label = None
    field_root = soup.select_one(".entity-field.grid")
    if field_root:
        for child in field_root.find_all("div", recursive=False):
            classes = set(child.get("class") or [])
            if "text-body-tertiary" in classes and "text-break" not in classes:
                current_label = clean_text(child)
            elif "text-break" in classes and current_label:
                fields[current_label] = clean_text(child)
                current_label = None

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            parsed = json.loads(script.string or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed.get("@type") in {"Movie", "TVSeries", "CreativeWork"}:
            schema_data = parsed
            break

    aggregate = schema_data.get("aggregateRating") if isinstance(schema_data, dict) else None
    aggregate = aggregate if isinstance(aggregate, dict) else {}
    player_shell = soup.select_one(".player__video[data-ajax-url]")
    player_url = player_shell.get("data-ajax-url") if player_shell else None
    poster = soup.select_one(".entity__poster img.image__img")
    aggregate_score = parse_score(soup.select_one(".entity-rating__aggregate-score .entity-rating__value"))
    aggregate_count = parse_int(soup.select_one(".entity-rating__count"))

    genres = [clean_text(a) for a in soup.select(".entity-field__genres a[href^='/anime/genre/']")]
    genres = [g for g in genres if g]
    dubbings = [clean_text(a) for a in soup.select("a[href^='/anime/dubbing/']")]
    dubbings = [d for d in dubbings if d]

    return {
        "title": clean_text(soup.select_one("h1")),
        "cover_url": absolute_url(poster.get("src")) if poster else None,
        "description": clean_text(soup.select_one(".description")),
        "fields": fields,
        "schema_data": schema_data,
        "aggregate_score": aggregate_score if aggregate_score is not None else parse_score(aggregate.get("ratingValue")),
        "aggregate_count": aggregate_count if aggregate_count is not None else parse_int(aggregate.get("ratingCount")),
        "content_rating": schema_data.get("contentRating") if isinstance(schema_data, dict) else None,
        "date_published": schema_data.get("datePublished") if isinstance(schema_data, dict) else None,
        "genres": genres,
        "dubbings": dubbings,
        "player_url": player_url,
    }


def normalize_embed_url(url):
    if not url:
        return None
    return html.unescape(url).replace("&amp;", "&")


def credential_free_netloc(parsed):
    if parsed.username is not None or parsed.password is not None or not parsed.hostname:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    hostname = parsed.hostname
    host = f"[{hostname}]" if ":" in hostname else hostname
    return f"{host}:{port}" if port is not None else host


def redact_embed_url(url):
    url = normalize_embed_url(url)
    if not url:
        return None
    parsed = urlparse("https:" + url if url.startswith("//") else url)
    host = credential_free_netloc(parsed)
    if not host:
        return None
    path = parsed.path
    if "aniboom.one" in host:
        path = re.sub(r"(/embed/)[^/?#]+", r"\1<redacted>", path)
        query = urlencode([(k, v if k in {"episode", "translation"} else "<redacted>") for k, v in parse_qsl(parsed.query)])
    elif "kodikplayer.com" in host.lower():
        path = re.sub(
            r"(/(?:seria|serial|season|video)(?:/\d+)?/)[^/?#]+",
            r"\1<redacted>",
            path,
            flags=re.IGNORECASE,
        )
        query = urlencode([(key, "<redacted>") for key, _ in parse_qsl(parsed.query)])
    else:
        path = re.sub(r"/[A-Za-z0-9_-]{8,}", "/<redacted>", path)
        query = "<redacted>" if parsed.query else ""
    redacted = urlunparse(("", host, path, "", query, ""))
    return "//" + redacted.lstrip("/")


def embed_host(url):
    url = normalize_embed_url(url)
    if not url:
        return None
    parsed = urlparse("https:" + url if url.startswith("//") else url)
    return credential_free_netloc(parsed)


def parse_player_content(content):
    soup = BeautifulSoup(content or "", "lxml")
    selected_option = soup.select_one("select[name='series'] option[selected]")
    selected_episode_id = int(selected_option.get("value")) if selected_option and selected_option.get("value", "").isdigit() else None

    episodes = []
    for node in soup.select(".player-video-bar__item[data-episode]"):
        episode_id = node.get("data-episode")
        if not episode_id or not episode_id.isdigit():
            continue
        episodes.append(
            {
                "id": int(episode_id),
                "number": node.get("data-episode-number"),
                "episode_type": node.get("data-episode-type"),
                "title": node.get("data-episode-title"),
                "release_label": node.get("data-episode-released"),
                "description": node.get("data-episode-description"),
            }
        )

    translations = []
    for node in soup.select("[data-translation]"):
        translation_id = node.get("data-translation")
        if translation_id and translation_id.isdigit():
            translations.append({"id": int(translation_id), "title": clean_text(node)})

    providers = []
    for node in soup.select("[data-player][data-provider][data-ptranslation]"):
        raw_url = normalize_embed_url(node.get("data-player"))
        translation_id = node.get("data-ptranslation")
        providers.append(
            {
                "provider_id": node.get("data-provider"),
                "provider_title": node.get("data-provider-title"),
                "translation_id": int(translation_id) if translation_id and translation_id.isdigit() else 0,
                "translation_title": node.get("data-translation-title") or "unknown",
                "embed_url": raw_url,
                "embed_url_redacted": redact_embed_url(raw_url),
                "embed_host": embed_host(raw_url),
            }
        )
    return selected_episode_id, episodes, translations, providers


def parse_unavailable_reason(content):
    text = BeautifulSoup(content or "", "lxml").get_text(" ", strip=True)
    return " ".join(text.split()) or None


def synthetic_episode(anime_id, title):
    return {
        "id": int(anime_id) * 1000 + 1,
        "number": "1",
        "episode_type": "movie",
        "title": title,
        "release_label": None,
        "description": "Single player entry without an upstream episode list.",
    }


def selected_episodes(episodes, episode_limit):
    if episode_limit == 0:
        return episodes
    return episodes[:episode_limit]


def init_db(db_path):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute("pragma foreign_keys=on")
    con.execute("pragma busy_timeout=30000")
    con.executescript(
        """
        create table if not exists anime (
            id integer primary key,
            slug text,
            title text not null,
            subtitle text,
            url text not null unique,
            cover_url text,
            source text,
            source_id text,
            listing_score real,
            aggregate_score real,
            aggregate_count integer,
            date_published text,
            kind text,
            year text,
            status text,
            episodes_text text,
            release_text text,
            rating text,
            age text,
            duration text,
            studio text,
            season text,
            description text,
            fields_json text,
            schema_json text,
            scraped_at text not null
        );

        create table if not exists anime_fields (
            anime_id integer not null references anime(id) on delete cascade,
            label text not null,
            value text,
            primary key (anime_id, label)
        );

        create table if not exists anime_title_aliases (
            anime_id integer not null references anime(id) on delete cascade,
            alias text not null,
            normalized_alias text not null,
            language text,
            alias_type text not null default 'alias',
            source text not null default 'manual',
            source_ref text,
            confidence real not null default 1.0,
            created_at text not null,
            updated_at text not null,
            primary key (anime_id, normalized_alias, source, alias_type)
        );

        create index if not exists idx_anime_title_aliases_anime_id
            on anime_title_aliases(anime_id);

        create index if not exists idx_anime_title_aliases_normalized
            on anime_title_aliases(normalized_alias);

        create table if not exists anime_genres (
            anime_id integer not null references anime(id) on delete cascade,
            genre text not null,
            primary key (anime_id, genre)
        );

        create table if not exists anime_dubbings (
            anime_id integer not null references anime(id) on delete cascade,
            dubbing text not null,
            primary key (anime_id, dubbing)
        );

        create table if not exists episodes (
            id integer primary key,
            anime_id integer not null references anime(id) on delete cascade,
            number text,
            title text,
            release_label text,
            episode_type text,
            description text,
            has_video integer not null default 0,
            unavailable_reason text,
            scraped_at text not null
        );

        create table if not exists translations (
            id integer primary key,
            title text not null
        );

        create table if not exists video_sources (
            id integer primary key autoincrement,
            anime_id integer not null references anime(id) on delete cascade,
            episode_id integer not null references episodes(id) on delete cascade,
            provider_id text,
            provider_title text,
            translation_id integer,
            translation_title text,
            embed_host text,
            embed_url text,
            embed_url_redacted text,
            scraped_at text not null,
            unique (episode_id, provider_id, translation_id, embed_url_redacted)
        );

        create table if not exists scrape_runs (
            id integer primary key autoincrement,
            start_url text not null,
            created_at text not null,
            anime_count integer not null,
            episode_count integer not null,
            video_source_count integer not null,
            include_embed_urls integer not null
        );

        create index if not exists idx_video_sources_provider_playable
            on video_sources(provider_id)
            where embed_url is not null;
        """
    )
    ensure_columns(
        con,
        "anime",
        {
            "listing_score": "real",
            "aggregate_score": "real",
            "aggregate_count": "integer",
            "date_published": "text",
            "fields_json": "text",
            "schema_json": "text",
            "source": "text",
            "source_id": "text",
        },
    )
    con.execute("update anime set source = 'animego' where source is null")
    con.execute("update anime set source_id = cast(id as text) where source_id is null")
    return con


def ensure_columns(con, table, columns):
    existing = {row[1] for row in con.execute(f"pragma table_info({table})")}
    for column, definition in columns.items():
        if column not in existing:
            con.execute(f"alter table {table} add column {column} {definition}")


def upsert_anime(con, item, detail, scraped_at, *, authoritative_metadata):
    fields = detail["fields"]
    fields_json = json.dumps(fields, ensure_ascii=False, sort_keys=True)
    schema_json = json.dumps(detail.get("schema_data") or {}, ensure_ascii=False, sort_keys=True)
    if authoritative_metadata:
        conflict_updates = """
            slug=excluded.slug,
            title=excluded.title,
            subtitle=excluded.subtitle,
            url=excluded.url,
            cover_url=excluded.cover_url,
            source=excluded.source,
            source_id=excluded.source_id,
            listing_score=excluded.listing_score,
            aggregate_score=excluded.aggregate_score,
            aggregate_count=excluded.aggregate_count,
            date_published=excluded.date_published,
            kind=excluded.kind,
            year=excluded.year,
            status=excluded.status,
            episodes_text=excluded.episodes_text,
            release_text=excluded.release_text,
            rating=excluded.rating,
            age=excluded.age,
            duration=excluded.duration,
            studio=excluded.studio,
            season=excluded.season,
            description=excluded.description,
            fields_json=excluded.fields_json,
            schema_json=excluded.schema_json,
            scraped_at=excluded.scraped_at
        """
    else:
        conflict_updates = """
            slug=excluded.slug,
            title=coalesce(nullif(excluded.title, ''), anime.title),
            subtitle=coalesce(excluded.subtitle, anime.subtitle),
            url=excluded.url,
            cover_url=coalesce(excluded.cover_url, anime.cover_url),
            source=coalesce(excluded.source, anime.source),
            source_id=coalesce(excluded.source_id, anime.source_id),
            listing_score=coalesce(excluded.listing_score, anime.listing_score),
            aggregate_score=coalesce(excluded.aggregate_score, anime.aggregate_score),
            aggregate_count=coalesce(excluded.aggregate_count, anime.aggregate_count),
            date_published=coalesce(excluded.date_published, anime.date_published),
            kind=coalesce(excluded.kind, anime.kind),
            year=coalesce(excluded.year, anime.year),
            status=coalesce(excluded.status, anime.status),
            episodes_text=coalesce(excluded.episodes_text, anime.episodes_text),
            release_text=coalesce(excluded.release_text, anime.release_text),
            rating=coalesce(excluded.rating, anime.rating),
            age=coalesce(excluded.age, anime.age),
            duration=coalesce(excluded.duration, anime.duration),
            studio=coalesce(excluded.studio, anime.studio),
            season=coalesce(excluded.season, anime.season),
            description=coalesce(excluded.description, anime.description),
            fields_json=case
                when excluded.fields_json is null or excluded.fields_json = '{}' then anime.fields_json
                else excluded.fields_json
            end,
            schema_json=case
                when excluded.schema_json is null or excluded.schema_json = '{}' then anime.schema_json
                else excluded.schema_json
            end,
            scraped_at=excluded.scraped_at
        """
    con.execute(
        f"""
        insert into anime (
            id, slug, title, subtitle, url, cover_url, source, source_id, listing_score,
            aggregate_score, aggregate_count, date_published, kind, year, status,
            episodes_text, release_text, rating, age, duration, studio, season,
            description, fields_json, schema_json, scraped_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(id) do update set {conflict_updates}
        """,
        (
            item["id"],
            item["slug"],
            detail["title"] or item["title"],
            item.get("subtitle"),
            item["url"],
            detail["cover_url"] or item.get("cover_url"),
            item.get("source", "animego"),
            str(item.get("source_id", item["id"])),
            item.get("listing_score"),
            detail.get("aggregate_score"),
            detail.get("aggregate_count"),
            detail.get("date_published"),
            fields.get("Тип") or item.get("kind"),
            item.get("year"),
            fields.get("Статус"),
            fields.get("Эпизоды"),
            fields.get("Выпуск"),
            detail.get("content_rating") or fields.get("Рейтинг"),
            fields.get("Возраст"),
            fields.get("Длительность"),
            fields.get("Студия"),
            fields.get("Сезон"),
            detail["description"] or item.get("listing_description"),
            fields_json,
            schema_json,
            scraped_at,
        ),
    )
    if authoritative_metadata or fields:
        con.execute("delete from anime_fields where anime_id=?", (item["id"],))
        for label, value in sorted(fields.items()):
            con.execute(
                "insert or replace into anime_fields(anime_id, label, value) values (?, ?, ?)",
                (item["id"], label, value),
            )
    genres = sorted(set(detail["genres"] or item.get("genres", [])))
    if authoritative_metadata or genres:
        con.execute("delete from anime_genres where anime_id=?", (item["id"],))
        for genre in genres:
            con.execute("insert or ignore into anime_genres(anime_id, genre) values (?, ?)", (item["id"], genre))
    dubbings = sorted(set(detail["dubbings"]))
    if authoritative_metadata or dubbings:
        con.execute("delete from anime_dubbings where anime_id=?", (item["id"],))
        for dubbing in dubbings:
            con.execute("insert or ignore into anime_dubbings(anime_id, dubbing) values (?, ?)", (item["id"], dubbing))


def upsert_episode(con, anime_id, episode, has_video, unavailable_reason, scraped_at):
    con.execute(
        """
        insert into episodes (
            id, anime_id, number, title, release_label, episode_type,
            description, has_video, unavailable_reason, scraped_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(id) do update set
            anime_id=excluded.anime_id,
            number=excluded.number,
            title=excluded.title,
            release_label=excluded.release_label,
            episode_type=excluded.episode_type,
            description=excluded.description,
            has_video=excluded.has_video,
            unavailable_reason=excluded.unavailable_reason,
            scraped_at=excluded.scraped_at
        """,
        (
            episode["id"],
            anime_id,
            episode.get("number"),
            episode.get("title"),
            episode.get("release_label"),
            episode.get("episode_type"),
            episode.get("description"),
            1 if has_video else 0,
            unavailable_reason,
            scraped_at,
        ),
    )


def upsert_provider(con, anime_id, episode_id, provider, include_embed_urls, scraped_at):
    raw_embed_url = normalize_embed_url(provider.get("embed_url"))
    if raw_embed_url:
        parsed_embed_url = urlparse("https:" + raw_embed_url if raw_embed_url.startswith("//") else raw_embed_url)
        if parsed_embed_url.username is not None or parsed_embed_url.password is not None:
            raise ValueError("player URL credentials are not allowed")
    con.execute(
        "insert or ignore into translations(id, title) values (?, ?)",
        (provider["translation_id"], provider["translation_title"] or str(provider["translation_id"])),
    )
    con.execute(
        """
        insert into video_sources (
            anime_id, episode_id, provider_id, provider_title, translation_id,
            translation_title, embed_host, embed_url, embed_url_redacted, scraped_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict do update set
            anime_id=excluded.anime_id,
            provider_title=coalesce(excluded.provider_title, video_sources.provider_title),
            translation_title=coalesce(excluded.translation_title, video_sources.translation_title),
            embed_host=coalesce(excluded.embed_host, video_sources.embed_host),
            embed_url=coalesce(excluded.embed_url, video_sources.embed_url),
            embed_url_redacted=coalesce(excluded.embed_url_redacted, video_sources.embed_url_redacted),
            scraped_at=excluded.scraped_at
        """,
        (
            anime_id,
            episode_id,
            provider["provider_id"],
            provider["provider_title"],
            provider["translation_id"],
            provider["translation_title"],
            provider["embed_host"],
            provider["embed_url"] if include_embed_urls else None,
            provider["embed_url_redacted"],
            scraped_at,
        ),
    )


def _scrape(args):
    con = init_db(args.db)
    scraped_at = now_iso()
    listing_items = []
    seen_ids = set()

    source_url = args.start_url.rstrip("/")
    page = 1
    max_pages = args.max_pages if args.all_pages else args.pages
    while page <= max_pages:
        page_url = source_url if page == 1 else f"{source_url}/{page}"
        try:
            page_html = fetch_text(page_url, delay=args.delay)
        except HTTPError as exc:
            code = exc.code
            exc.close()
            if args.all_pages and code == 404:
                print(f"page {page}: 404, stopping")
                break
            raise
        page_items = parse_listing(page_html)
        new_items = [item for item in page_items if item["id"] not in seen_ids]
        for item in new_items:
            seen_ids.add(item["id"])
            listing_items.append(item)
        print(f"page {page}: {len(new_items)} new anime")
        if not args.all_pages and len(listing_items) >= args.limit:
            break
        if args.all_pages and not new_items:
            break
        if args.limit and len(listing_items) >= args.limit:
            break
        page += 1

    if args.limit:
        listing_items = listing_items[: args.limit]
    episode_count = 0
    source_count = 0

    for index, item in enumerate(listing_items, start=1):
        print(f"[{index}/{len(listing_items)}] {item['title']}")
        detail = parse_detail(fetch_text(item["url"], delay=args.delay))
        upsert_anime(con, item, detail, scraped_at, authoritative_metadata=True)

        if not detail["player_url"]:
            con.commit()
            continue

        if args.skip_player:
            con.commit()
            continue

        player_json = json.loads(fetch_text(detail["player_url"], ajax=True, referer=item["url"], delay=args.delay))
        content = player_json.get("data", {}).get("content") or ""
        selected_episode_id, episodes, _, initial_providers = parse_player_content(content)

        if not episodes and initial_providers:
            episodes = [synthetic_episode(item["id"], item["title"])]

        for episode in selected_episodes(episodes, args.episode_limit):
            if initial_providers and (
                episode["id"] == int(item["id"]) * 1000 + 1
                or selected_episode_id == episode["id"]
                or len(episodes) == 1
            ):
                providers = initial_providers
                unavailable_reason = None
                has_video = True
            else:
                video_json = json.loads(fetch_text(f"/player/videos/{episode['id']}", ajax=True, referer=item["url"], delay=args.delay))
                data = video_json.get("data", {})
                video_content = data.get("content") or ""
                _, _, _, providers = parse_player_content(video_content)
                unavailable_reason = parse_unavailable_reason(data.get("content_online") or "")
                has_video = bool(data.get("numVideos", 0) or providers)
            upsert_episode(con, item["id"], episode, has_video, unavailable_reason, scraped_at)
            episode_count += 1
            for provider in providers:
                upsert_provider(con, item["id"], episode["id"], provider, args.include_embed_urls, scraped_at)
                source_count += 1
            print(f"  episode {episode.get('number')}: {len(providers)} provider options")

        if args.episode_limit:
            for episode in episodes[args.episode_limit :]:
                upsert_episode(con, item["id"], episode, False, "not fetched in this sample run", scraped_at)

        con.commit()

    con.execute(
        """
        insert into scrape_runs(start_url, created_at, anime_count, episode_count, video_source_count, include_embed_urls)
        values (?, ?, ?, ?, ?, ?)
        """,
        (source_url, scraped_at, len(listing_items), episode_count, source_count, 1 if args.include_embed_urls else 0),
    )
    con.commit()
    con.close()
    print(f"saved {len(listing_items)} anime, {episode_count} fetched episodes, {source_count} provider rows to {args.db}")


def scrape(args):
    with DatabaseOperationLock(
        args.db,
        path=getattr(args, "lock_file", None) or default_lock_path(args.db),
        wait=getattr(args, "wait_lock", False),
        timeout=getattr(args, "lock_timeout", 30.0),
        operation="AnimeGO scrape",
    ):
        return _scrape(args)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Scrape a small AnimeGO metadata sample into SQLite.")
    parser.add_argument("--db", default="db/animego.sqlite")
    parser.add_argument("--start-url", default=START_URL)
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--all-pages", action="store_true")
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--episode-limit", type=int, default=0, help="episodes per title to fetch; 0 means all")
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--include-embed-urls", action="store_true")
    parser.add_argument("--skip-player", action="store_true")
    parser.add_argument("--lock-file")
    parser.add_argument("--wait-lock", action="store_true")
    parser.add_argument("--lock-timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    scrape(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
