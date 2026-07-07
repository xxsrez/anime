#!/usr/bin/env python3
import argparse
import json
import re
import time
import zlib
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urljoin, urlparse, parse_qsl, urlunparse
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

from scrape_animego import (
    clean_text,
    embed_host,
    init_db,
    normalize_embed_url,
    now_iso,
    parse_int,
    parse_score,
    redact_embed_url,
    upsert_anime,
    upsert_episode,
    upsert_provider,
)


BASE_URL = "https://yummyanime.tv"
YUMMYANI_BASE_URL = "https://ru.yummyani.me"
YUMMYANI_API_BASE = "https://api.yani.tv"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
YUMMY_ID_OFFSET = 10_000_000
YUMMY_TRANSLATION_ID = 9_000_001
YUMMYANI_TRANSLATION_ID_OFFSET = 9_100_000

DEFAULT_URLS = [
    "https://yummyanime.tv/267-reinkarnacija-bezrabotnogo-istorija-o-prikljuchenijah-v-drugom-mire-k1.html",
    "https://yummyanime.tv/1227-reinkarnacija-bezrabotnogo-istorija-o-prikljuchenijah-v-drugom-mire-specvypusk-e1.html",
    "https://yummyanime.tv/4483-reinkarnacija-bezrabotnogo-istorija-o-prikljuchenijah-v-drugom-mire-2-mag-hranitel-fitc.html",
    "https://yummyanime.tv/4934-reinkarnacija-bezrabotnogo-istorija-o-prikljuchenijah-v-drugom-mire-3.html",
]


def absolute_url(url):
    return urljoin(BASE_URL, url) if url else None


def fetch_text(url, referer=None, delay=0.0):
    if delay:
        time.sleep(delay)
    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
    req = Request(absolute_url(url), headers=headers)
    with urlopen(req, timeout=30) as response:
        return response.read().decode("utf-8", "replace")


def fetch_json(url, referer=None, delay=0.0):
    return json.loads(fetch_text(url, referer=referer, delay=delay))


def parse_yummy_source_id(url):
    match = re.search(r"/(\d+)-", urlparse(url).path)
    return int(match.group(1)) if match else None


def internal_anime_id(source_id):
    return YUMMY_ID_OFFSET + int(source_id)


def internal_episode_id(anime_id, number):
    return anime_id * 1000 + int(number)


def parse_slug(url):
    return urlparse(url).path.rsplit("/", 1)[-1].replace(".html", "")


def catalog_year_url(year, page=1):
    if page == 1:
        return f"{BASE_URL}/catalog/{year}/"
    return f"{BASE_URL}/catalog/{year}/page/{page}/"


def parse_catalog_listing(html_text, page_url):
    soup = BeautifulSoup(html_text, "lxml")
    urls = []
    for link in soup.select("#dle-content .movie-item__link[href]"):
        url = urljoin(page_url, link.get("href"))
        if re.search(r"/\d+-.+\.html$", urlparse(url).path):
            urls.append(url)
    return urls


def collect_catalog_urls(years, max_pages, delay=0.0):
    urls = []
    seen_urls = set()
    for year in years:
        year_count = 0
        for page in range(1, max_pages + 1):
            page_url = catalog_year_url(year, page)
            try:
                page_html = fetch_text(page_url, delay=delay)
            except HTTPError as exc:
                if exc.code == 404:
                    print(f"catalog {year} page {page}: 404, stopping")
                    break
                raise
            page_urls = parse_catalog_listing(page_html, page_url)
            new_urls = [url for url in page_urls if url not in seen_urls]
            for url in new_urls:
                seen_urls.add(url)
                urls.append(url)
            year_count += len(new_urls)
            print(f"catalog {year} page {page}: {len(new_urls)} new titles")
            if not page_urls or not new_urls:
                break
        print(f"catalog {year}: {year_count} collected titles")
    return urls


def asset_url(url):
    if not url:
        return None
    if url.startswith("//"):
        return "https:" + url
    return urljoin(YUMMYANI_BASE_URL, url)


def is_modern_yummyani_url(url):
    parsed = urlparse(url)
    return parsed.netloc.lower().endswith("yummyani.me") and "/catalog/item/" in parsed.path


def parse_modern_slug(url):
    slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    return slug.removesuffix(".html")


def modern_slug_candidates(slug):
    yield slug
    if re.search(r"-\d{4}-\d{2}-\d{2}$", slug):
        yield re.sub(r"-\d{4}-\d{2}-\d{2}$", "", slug)


def fetch_modern_anime(slug, include_embed_urls, delay=0.0):
    last_error = None
    for candidate in modern_slug_candidates(slug):
        api_url = f"{YUMMYANI_API_BASE}/anime/{quote(candidate)}?need_videos={'true' if include_embed_urls else 'false'}"
        try:
            payload = fetch_json(api_url, delay=delay)
        except Exception as exc:
            last_error = exc
            continue
        anime = payload.get("response")
        if anime:
            return anime, api_url
        last_error = ValueError(payload.get("error") or f"YummyAni API returned no anime for {candidate}")
    raise ValueError(f"YummyAni title page not available: {slug}") from last_error


def format_duration(seconds):
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    minutes = round(seconds / 60)
    return f"{minutes} мин."


def first_latin_title(titles):
    return next((title for title in titles if re.search(r"[A-Za-z]", title or "")), None)


def translation_id_for_label(label):
    label = label or "YummyAni"
    return YUMMYANI_TRANSLATION_ID_OFFSET + (zlib.crc32(label.encode("utf-8")) % 900_000)


def modern_provider_title(raw_title):
    title = (raw_title or "YummyAni").strip()
    title = re.sub(r"^Плеер\s+", "", title, flags=re.I)
    return title or "YummyAni"


def should_skip_modern_provider(provider_title):
    return (provider_title or "").strip().lower() == "alloha"


def clean_field_value(label, value):
    if not value:
        return None
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(rf"^{re.escape(label)}\s*:?\s*", "", value, flags=re.I).strip()
    return value or None


def parse_info_fields(soup):
    fields = {}
    for node in soup.select(".inner-page__list > li"):
        label_node = node.find("span", recursive=False)
        label = clean_text(label_node)
        if not label:
            continue
        label = label.rstrip(":")
        value = clean_field_value(label, clean_text(node))
        if value:
            fields[label] = value
    age = clean_text(soup.select_one(".inner-page__img .movie-item__age"))
    if age:
        fields["Возраст"] = age
    shikimori = clean_text(soup.select_one(".rate__shiki span"))
    if shikimori:
        fields["Shikimori"] = shikimori
    imdb = clean_text(soup.select_one(".rate__imdb span"))
    if imdb:
        fields["IMDB"] = imdb
    return fields


def parse_rating(soup, fields):
    rating_value = parse_score(soup.select_one('[itemprop="ratingValue"]'))
    rating_count = parse_int(soup.select_one('[itemprop="ratingCount"]'))
    if rating_value is None:
        rating_value = parse_score(fields.get("Рейтинг аниме"))
    if rating_count is None:
        rating_count = parse_int(fields.get("Рейтинг аниме"))
    return rating_value, rating_count


def parse_genres(soup):
    genres = []
    genre_root = soup.select_one('[itemprop="genre"]')
    for link in (genre_root.select("a") if genre_root else []):
        value = clean_text(link)
        if value:
            genres.append(value)
    return genres


def parse_dubbings(fields):
    value = fields.get("Озвучка от") or fields.get("Тип перевода") or ""
    return [part.strip() for part in re.split(r"\s*,\s*", value) if part.strip()]


def parse_episode_count(label, fields):
    count = parse_int(label) or parse_int(fields.get("Эпизоды"))
    return count or 1


def infer_kind(title):
    if any(marker in title for marker in ("Эрис", "Фитц")):
        return "Спешл"
    return "Сериал"


def parse_player_params(soup):
    players = []
    for node in soup.select(".xfplayer[data-params]"):
        raw_params = node.get("data-params") or ""
        params = dict(parse_qsl(raw_params.replace("&amp;", "&")))
        mod = params.get("mod") or "player"
        if mod == "kodik-player":
            title = "Kodik"
        elif mod == "alloha-player":
            continue
        else:
            title = mod
        players.append({"provider_id": f"yummy-{mod}", "provider_title": title, "params": raw_params})
    return players


def fetch_player_url(player, page_url, delay=0.0):
    data = json.loads(fetch_text(f"/engine/ajax/controller.php?{player['params']}", referer=page_url, delay=delay))
    if not data.get("success"):
        return None
    url = normalize_embed_url(data.get("data"))
    if not url:
        return None
    parsed = urlparse(url)
    if not parsed.path.strip("/") and not parsed.query:
        return None
    return url


def selected_option(select):
    if not select:
        return None
    return select.select_one("option[selected]") or select.select_one("option")


def kodik_serial_url(embed_url, serial_id, serial_hash, season_number, episode_number):
    parsed = urlparse("https:" + embed_url if embed_url.startswith("//") else embed_url)
    path = re.sub(
        r"^/serial/[^/]+/[^/]+/",
        f"/serial/{serial_id}/{serial_hash}/",
        parsed.path,
        count=1,
    )
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in {"season", "episode"}
    ]
    query.extend([("season", str(season_number)), ("episode", str(episode_number))])
    return urlunparse((parsed.scheme or "https", parsed.netloc, path, "", urlencode(query), parsed.fragment))


def parse_kodik_serial_episode_urls(embed_url, html_text):
    if not embed_url or "kodikplayer.com/serial/" not in embed_url:
        return []

    soup = BeautifulSoup(html_text or "", "lxml")
    season = selected_option(soup.select_one(".serial-seasons-box select"))
    episodes = soup.select(".serial-series-box select option")
    serial_id = season.get("data-serial-id") if season else None
    serial_hash = season.get("data-serial-hash") if season else None
    season_number = season.get("value") if season else None
    if not serial_id or not serial_hash or not season_number:
        return []

    urls = []
    for episode in episodes:
        query_episode = episode.get("value")
        visible_number = parse_int(episode.get("data-title")) or parse_int(clean_text(episode)) or parse_int(query_episode)
        if not query_episode or visible_number is None:
            continue
        urls.append(
            {
                "episode_number": str(visible_number),
                "embed_url": kodik_serial_url(embed_url, serial_id, serial_hash, season_number, query_episode),
            }
        )
    return urls


def title_season_number(title):
    match = re.search(r"(\d+)\s*сезон", title or "", flags=re.I)
    return match.group(1) if match else None


def kodik_season_option_url(embed_url, html_text, season_number):
    soup = BeautifulSoup(html_text or "", "lxml")
    for season in soup.select(".serial-seasons-box select option"):
        if season.get("value") != str(season_number):
            continue
        serial_id = season.get("data-serial-id")
        serial_hash = season.get("data-serial-hash")
        if serial_id and serial_hash:
            return kodik_serial_url(embed_url, serial_id, serial_hash, season.get("value"), 1)
    return None


def expand_legacy_provider_urls(player, embed_url, page_url, episode_count, delay=0.0, preferred_season=None):
    if player.get("provider_title") == "Kodik" and episode_count > 1:
        try:
            html_text = fetch_text(embed_url, referer=page_url, delay=delay)
        except Exception:
            html_text = ""
        episode_urls = parse_kodik_serial_episode_urls(embed_url, html_text)
        if preferred_season and episode_urls and len(episode_urls) != episode_count:
            preferred_url = kodik_season_option_url(embed_url, html_text, preferred_season)
            if preferred_url:
                try:
                    preferred_html = fetch_text(preferred_url, referer=page_url, delay=delay)
                except Exception:
                    preferred_html = ""
                preferred_episode_urls = parse_kodik_serial_episode_urls(preferred_url, preferred_html)
                if len(preferred_episode_urls) == episode_count:
                    return preferred_episode_urls
        if episode_urls:
            return episode_urls
    episode_number = "1" if episode_count > 1 else None
    return [{"episode_number": episode_number, "embed_url": embed_url}]


def parse_modern_detail(page_url, include_embed_urls=True, delay=0.0):
    slug = parse_modern_slug(page_url)
    anime, api_url = fetch_modern_anime(slug, include_embed_urls, delay=delay)
    source_id = anime.get("anime_id")
    if source_id is None:
        raise ValueError(f"Cannot parse YummyAni id: {page_url}")

    source_id = int(source_id)
    anime_id = internal_anime_id(source_id)
    rating = anime.get("rating") or {}
    episode_info = anime.get("episodes") or {}
    min_age = anime.get("min_age") or {}
    status = anime.get("anime_status") or {}
    anime_type = anime.get("type") or {}
    remote_ids = anime.get("remote_ids") or {}
    poster = anime.get("poster") or {}
    videos = sorted(anime.get("videos") or [], key=lambda item: (int(item.get("number") or 0), item.get("index") or 0))
    other_titles = anime.get("other_titles") or []

    episode_count = int(episode_info.get("count") or episode_info.get("aired") or 1)
    genres = [item["title"] for item in anime.get("genres") or [] if item.get("title")]
    dubbings = sorted(
        {
            (video.get("data") or {}).get("dubbing")
            for video in videos
            if (video.get("data") or {}).get("dubbing")
        }
    )
    if not dubbings:
        dubbings = [item["title"] for item in anime.get("translates") or [] if item.get("title")]

    fields = {
        "Источник": "YummyAni",
        "Source ID": str(source_id),
        "URL": page_url,
        "API URL": api_url,
        "Тип": anime_type.get("name") or "Аниме",
        "Статус": status.get("title"),
        "Эпизоды": str(episode_count),
        "Возраст": min_age.get("title_long") or min_age.get("title"),
        "Длительность": format_duration(anime.get("duration")),
        "Год выхода": str(anime.get("year")) if anime.get("year") else None,
        "Студия": ", ".join(item["title"] for item in anime.get("studios") or [] if item.get("title")) or None,
        "Режиссёр": ", ".join(item["title"] for item in anime.get("creators") or [] if item.get("title")) or None,
        "Рейтинг аниме": f"{rating.get('average'):.2f} ({rating.get('counters')})" if rating.get("average") else None,
        "MyAnimeList ID": str(remote_ids.get("myanimelist_id")) if remote_ids.get("myanimelist_id") else None,
        "Shikimori ID": str(remote_ids.get("shikimori_id")) if remote_ids.get("shikimori_id") else None,
        "Кинопоиск ID": str(remote_ids.get("kp_id")) if remote_ids.get("kp_id") else None,
        "Другие названия": "; ".join(other_titles) if other_titles else None,
    }
    fields = {key: value for key, value in fields.items() if value}

    cover_url = asset_url(poster.get("big") or poster.get("fullsize") or poster.get("huge"))
    canonical_url = f"{YUMMYANI_BASE_URL}/catalog/item/{anime.get('anime_url') or slug}"
    item = {
        "id": anime_id,
        "source": "yummyanime",
        "source_id": str(source_id),
        "slug": anime.get("anime_url") or slug,
        "title": anime.get("title") or slug,
        "subtitle": first_latin_title(other_titles),
        "url": canonical_url,
        "cover_url": cover_url,
        "listing_score": rating.get("average"),
        "kind": fields["Тип"],
        "year": str(anime.get("year")) if anime.get("year") else None,
        "genres": genres,
        "listing_description": anime.get("description"),
    }
    detail = {
        "title": item["title"],
        "cover_url": cover_url,
        "description": anime.get("description"),
        "fields": fields,
        "schema_data": {
            "source": "yummyani",
            "source_id": source_id,
            "url": canonical_url,
            "api_url": api_url,
            "remote_ids": remote_ids,
            "other_titles": other_titles,
        },
        "aggregate_score": rating.get("average"),
        "aggregate_count": rating.get("counters"),
        "content_rating": min_age.get("title_long") or min_age.get("title"),
        "date_published": None,
        "genres": genres,
        "dubbings": dubbings,
        "player_url": canonical_url if videos else None,
    }
    episodes = [
        {
            "id": internal_episode_id(anime_id, number),
            "number": str(number),
            "episode_type": "episode" if episode_count > 1 else "movie",
            "title": f"{number} серия" if episode_count > 1 else item["title"],
            "release_label": None,
            "description": "YummyAni iframe source.",
        }
        for number in range(1, episode_count + 1)
    ]

    providers = []
    for video in videos:
        embed_url = normalize_embed_url(video.get("iframe_url"))
        if not embed_url:
            continue
        data = video.get("data") or {}
        dubbing = data.get("dubbing") or "YummyAni"
        provider_title = modern_provider_title(data.get("player"))
        if should_skip_modern_provider(provider_title):
            continue
        providers.append(
            {
                "episode_number": str(video.get("number") or "1"),
                "provider_id": f"yummyani-{provider_title.lower()}-{video.get('video_id')}",
                "provider_title": provider_title,
                "translation_id": translation_id_for_label(dubbing),
                "translation_title": dubbing,
                "embed_url": embed_url,
                "embed_url_redacted": redact_embed_url(embed_url),
                "embed_host": embed_host(embed_url),
            }
        )
    return item, detail, episodes, providers


def parse_detail(page_url, html_text, include_embed_urls=True, skip_player=False, delay=0.0):
    if is_modern_yummyani_url(page_url):
        return parse_modern_detail(page_url, include_embed_urls=include_embed_urls and not skip_player, delay=delay)

    soup = BeautifulSoup(html_text, "lxml")
    title = clean_text(soup.select_one("h1"))
    if not title:
        raise ValueError(f"YummyAnime title page not available: {page_url}")

    source_id = parse_yummy_source_id(page_url)
    if source_id is None:
        raise ValueError(f"Cannot parse YummyAnime id: {page_url}")

    fields = parse_info_fields(soup)
    fields["Источник"] = "YummyAnime"
    fields["Source ID"] = str(source_id)
    fields["URL"] = page_url
    fields["Тип"] = infer_kind(title)

    label = clean_text(soup.select_one(".inner-page__img .movie-item__label"))
    if label and "сер" in label:
        fields["Эпизоды"] = str(parse_int(label) or label)
    elif label:
        fields["Статус"] = fields.get("Статус") or label

    subtitle = clean_text(soup.select_one(".inner-page__subtitle"))
    cover = soup.select_one(".inner-page__img img")
    description = clean_text(soup.select_one(".inner-page__desc .inner-page__text"))
    rating_value, rating_count = parse_rating(soup, fields)
    genres = parse_genres(soup)
    dubbings = parse_dubbings(fields)
    year = parse_int(fields.get("Год выхода"))
    episode_count = parse_episode_count(label, fields)
    anime_id = internal_anime_id(source_id)

    providers = []
    if not skip_player:
        for player in parse_player_params(soup):
            embed_url = fetch_player_url(player, page_url, delay=delay)
            if not embed_url:
                continue
            for episode_url in expand_legacy_provider_urls(
                player,
                embed_url,
                page_url,
                episode_count,
                delay=delay,
                preferred_season=title_season_number(title),
            ):
                episode_embed_url = episode_url["embed_url"]
                providers.append(
                    {
                        "episode_number": episode_url["episode_number"],
                        "provider_id": player["provider_id"],
                        "provider_title": player["provider_title"],
                        "translation_id": YUMMY_TRANSLATION_ID,
                        "translation_title": "YummyAnime",
                        "embed_url": episode_embed_url,
                        "embed_url_redacted": redact_embed_url(episode_embed_url),
                        "embed_host": embed_host(episode_embed_url),
                    }
                )

    item = {
        "id": anime_id,
        "source": "yummyanime",
        "source_id": str(source_id),
        "slug": parse_slug(page_url),
        "title": title,
        "subtitle": subtitle,
        "url": page_url,
        "cover_url": absolute_url(cover.get("src")) if cover else None,
        "listing_score": rating_value,
        "kind": fields["Тип"],
        "year": str(year) if year else None,
        "genres": genres,
        "listing_description": description,
    }
    detail = {
        "title": title,
        "cover_url": absolute_url(cover.get("src")) if cover else None,
        "description": description,
        "fields": fields,
        "schema_data": {
            "source": "yummyanime",
            "source_id": source_id,
            "url": page_url,
        },
        "aggregate_score": rating_value,
        "aggregate_count": rating_count,
        "content_rating": fields.get("Возраст"),
        "date_published": None,
        "genres": genres,
        "dubbings": dubbings,
        "player_url": page_url if providers else None,
    }
    episodes = [
        {
            "id": internal_episode_id(anime_id, number),
            "number": str(number),
            "episode_type": "episode",
            "title": f"{number} серия",
            "release_label": None,
            "description": "YummyAnime iframe contains the provider episode selector.",
        }
        for number in range(1, episode_count + 1)
    ]
    return item, detail, episodes, providers


def scrape(args):
    if args.catalog_years or args.from_year or args.to_year:
        if args.catalog_years:
            years = sorted(set(args.catalog_years), reverse=True)
        else:
            if not args.from_year or not args.to_year:
                raise ValueError("--from-year and --to-year must be used together")
            years = list(range(args.to_year, args.from_year - 1, -1))
        args.urls = collect_catalog_urls(years, args.max_pages, delay=args.delay)
        args.run_source = f"yummyanime:catalog:{min(years)}-{max(years)}"

    con = init_db(args.db)
    scraped_at = now_iso()
    source_count = 0
    episode_count = 0
    imported = 0

    for index, page_url in enumerate(args.urls, start=1):
        print(f"[{index}/{len(args.urls)}] {page_url}")
        html_text = fetch_text(page_url, delay=args.delay)
        item, detail, episodes, providers = parse_detail(
            page_url,
            html_text,
            include_embed_urls=not args.no_embed_urls,
            skip_player=args.skip_player,
            delay=args.delay,
        )
        print(f"  {item['title']}: {len(episodes)} episodes, {len(providers)} providers")
        upsert_anime(con, item, detail, scraped_at)

        if args.skip_player:
            con.commit()
            imported += 1
            continue

        for episode in episodes:
            episode_providers = [
                provider
                for provider in providers
                if provider.get("episode_number") in (None, str(episode.get("number")))
            ]
            upsert_episode(
                con,
                item["id"],
                episode,
                bool(episode_providers),
                None if episode_providers else "player not found",
                scraped_at,
            )
            episode_count += 1
            for provider in episode_providers:
                upsert_provider(con, item["id"], episode["id"], provider, not args.no_embed_urls, scraped_at)
                source_count += 1
        con.commit()
        imported += 1

    con.execute(
        """
        insert into scrape_runs(start_url, created_at, anime_count, episode_count, video_source_count, include_embed_urls)
        values (?, ?, ?, ?, ?, ?)
        """,
        (
            getattr(args, "run_source", None) or ",".join(args.urls),
            scraped_at,
            imported,
            episode_count,
            source_count,
            0 if args.no_embed_urls or args.skip_player else 1,
        ),
    )
    con.commit()
    con.close()
    print(f"saved {imported} YummyAnime titles, {episode_count} episodes, {source_count} provider rows to {args.db}")


def main():
    parser = argparse.ArgumentParser(description="Scrape selected YummyAnime title pages into the local SQLite app.")
    parser.add_argument("--db", default="db/animego.sqlite")
    parser.add_argument("--delay", type=float, default=0.25)
    parser.add_argument("--no-embed-urls", action="store_true")
    parser.add_argument("--skip-player", action="store_true")
    parser.add_argument("--catalog-years", nargs="+", type=int)
    parser.add_argument("--from-year", type=int)
    parser.add_argument("--to-year", type=int)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("urls", nargs="*", default=DEFAULT_URLS)
    args = parser.parse_args()
    scrape(args)


if __name__ == "__main__":
    main()
