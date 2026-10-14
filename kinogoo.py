"""Explicit Kinogoo imports; never infer playable episodes from page counters."""

import argparse
import datetime as dt
import json
import re
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse

from bs4 import BeautifulSoup
import requests

import scrape_animego as catalog
from scripts.operation_lock import DatabaseOperationLock

TITLE_OFFSET = 30_000_000
EPISODE_OFFSET = 1_500_000_000
PLAYER_HOST = "api.nextembed.ws"


def title_identity(source_id, season):
    if type(season) is not int or not 1 <= season <= 99:
        raise ValueError("Season must be between 1 and 99")
    if season == 1:
        return TITLE_OFFSET + source_id, str(source_id)
    return 40_000_000 + source_id * 100 + season, f"{source_id}:season:{season}"


def page_id(url):
    p = urlparse(url)
    match = re.fullmatch(r"/(\d+)-[a-z0-9-]+\.html", p.path)
    if p.scheme != "https" or p.netloc != "kinogoo.fm" or not match or p.query or p.fragment:
        raise ValueError("Expected a Kinogoo title URL")
    value = int(match[1])
    if not 0 < value < 1_000_000:
        raise ValueError("Kinogoo title ID out of range")
    return value


def player_url(html, url):
    page_id(url)
    soup = BeautifulSoup(html, "html.parser")
    for frame in soup.select("iframe[src]"):
        target = urljoin(url, frame["src"])
        p = urlparse(target)
        if p.scheme == "https" and p.netloc == PLAYER_HOST and re.fullmatch(r"/embed/kp/\d+", p.path):
            return target
    raise ValueError("No supported Kinogoo player found")


def parse_episodes(html, embed, season):
    match = re.search(r"\bseasons\s*:\s*(\[)", html)
    if not match:
        raise ValueError("Player playlist missing")
    seasons, _ = json.JSONDecoder().raw_decode(html[match.start(1):])
    result = []
    seen = set()
    for entry in seasons:
        if entry.get("season") != season or entry.get("blocked"):
            continue
        for ep in entry.get("episodes", []):
            if ep.get("blocked") or not (ep.get("hls") or ep.get("dash")):
                continue
            raw_id = ep.get("id")
            number = str(ep.get("episode", ""))
            if type(raw_id) is not int or not 0 < raw_id < 500_000_000 or not re.fullmatch(r"[1-9]\d*", number):
                raise ValueError("Invalid playlist episode")
            episode_id = EPISODE_OFFSET + raw_id
            if episode_id in seen:
                raise ValueError("Duplicate playlist episode")
            seen.add(episode_id)
            p = urlparse(embed)
            params = dict(parse_qsl(p.query))
            params.update(season=str(season), episode=number)
            url = urlunparse(p._replace(query=urlencode(params)))
            result.append({"episode": {"id": episode_id, "number": number, "title": f"Серия {number}"},
                           "providers": [{"provider_id": "kinogoo-nextembed", "provider_title": "Kinogoo",
                                          "translation_id": 0, "translation_title": "Оригинал",
                                          "embed_host": PLAYER_HOST, "embed_url": url,
                                          "embed_url_redacted": catalog.redact_embed_url(url)}]})
    if not result:
        raise ValueError("No playable episodes for the requested season")
    return result


def import_title(db, url, season):
    source_id = page_id(url)
    anime_id, source_key = title_identity(source_id, season)
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    html = response.text
    embed = player_url(html, url)
    response = requests.get(embed, timeout=30, headers={"Referer": url})
    response.raise_for_status()
    episodes = parse_episodes(response.text, embed, season)
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one("h1")
    title = heading.get_text(" ", strip=True) if heading else ""
    year = re.search(r"\((\d{4})\)\s*$", title)
    if not title or not year:
        raise ValueError("Missing title/year")
    title = title[:year.start()].strip()
    base_title = re.sub(r"\s+\d+(?:\s*[-–]\s*\d+)?\s+сезон(?:ы|ов)?$", "", title).strip()
    title = f"{base_title} {season} сезон"
    poster = soup.select_one('meta[property="og:image"]')
    if not poster:
        poster = soup.select_one('.full-poster img, .fposter img, .poster img')
    cover = urljoin(url, poster.get("content") or poster.get("src")) if poster else None
    timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
    with DatabaseOperationLock(db, operation="import Kinogoo title"):
        con = catalog.init_db(db)
        try:
            existing = con.execute("select source, source_id from anime where id=?", (anime_id,)).fetchone()
            if existing and tuple(existing) != ("kinogoo", source_key):
                raise ValueError("Title ID collision")
            item = {"id": anime_id, "source": "kinogoo", "source_id": source_key,
                    "slug": urlparse(url).path[1:-5] + (f"-season-{season}" if season > 1 else ""),
                    # The catalog requires unique URLs. A fragment distinguishes
                    # seasons without changing the upstream HTTP request.
                    "url": url + (f"#season-{season}" if season > 1 else ""),
                    "title": title, "year": year[1] if season == 1 else None}
            description = soup.select_one('.kino-h-sub2 p')
            detail = {"title": title, "cover_url": cover,
                      "description": description.get_text(" ", strip=True) if description and season == 1 else None,
                      "genres": [], "dubbings": [],
                      "fields": {"Тип": "Сериал", "Kinogoo season": str(season),
                                 **({"Выпуск": year[1]} if season == 1 else {})}}
            catalog.upsert_anime(con, item, detail, timestamp, authoritative_metadata=False)
            for snapshot in episodes:
                ep = snapshot["episode"]
                owner = con.execute("select anime_id from episodes where id=?", (ep["id"],)).fetchone()
                if owner and owner[0] != anime_id:
                    raise ValueError("Episode ID collision")
                catalog.upsert_episode(con, anime_id, ep, True, None, timestamp)
                for provider in snapshot["providers"]:
                    catalog.upsert_provider(con, anime_id, ep["id"], provider, True, timestamp)
            con.commit()
        finally:
            con.close()
    return {"id": anime_id, "title": title, "episodes": len(episodes)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--db", default="db/animego.sqlite")
    parser.add_argument("--season", type=int, required=True)
    args = parser.parse_args()
    print(json.dumps(import_title(args.db, args.url, args.season), ensure_ascii=False))
