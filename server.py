#!/usr/bin/env python3
import argparse
import base64
import binascii
import datetime as dt
import gzip
import hashlib
import html
import hmac
import json
import logging
import math
import mimetypes
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import unicodedata
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

import content_updates
import recommendation_model
import user_state_model
from scripts.operation_lock import DatabaseOperationLock, OperationLockError

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "db" / "animego.sqlite"
STATIC_DIR = ROOT / "static"
DEFAULT_LOG_DIR = ROOT / "data" / "logs"
DEFAULT_RECOMMENDATION_LIMIT = 20
MAX_RECOMMENDATION_LIMIT = 50
DEFAULT_CONTENT_UPDATE_DAYS = 7
DEFAULT_CONTENT_UPDATE_LIMIT = 160
MAX_CONTENT_UPDATE_LIMIT = 500
MAX_SEARCH_QUERY_CHARS = 240
MAX_SEARCH_QUERY_TOKENS = 12
MAX_JSON_BODY_BYTES = 64 * 1024
MAX_ANIMEGO_PUSH_BODY_BYTES = 32 * 1024 * 1024
MAX_CLIENT_ERROR_BYTES = 16 * 1024
MAX_PERFORMANCE_EVENT_BYTES = 24 * 1024
MAX_WATCH_METADATA_BYTES = 4096
MAX_CLIENT_ERROR_TEXT = 2048
MAX_CLIENT_ERROR_COLLECTION_ITEMS = 20
SYNC_MODES = {"hourly", "daily", "full"}
CONTENT_SYNC_SOURCES = ("yummyanime", "animego")
CONTENT_SYNC_BUSY_RETRY_SECONDS = 5 * 60
CONTENT_SYNC_ERROR_RETRY_SECONDS = 30 * 60
TRUTHY_VALUES = {"1", "true", "yes", "on"}
SYNTHETIC_RATING_PRIOR = 6.8
SYNTHETIC_RATING_MIN_COUNT = 80
SESSION_COOKIE_NAME = "anime_session"
MAX_SESSION_COOKIE_CANDIDATES = 8
MAX_SESSION_TOKEN_CHARS = 512
SESSION_TTL_SECONDS = 60 * 60 * 24 * 30
SESSION_LAST_SEEN_WRITE_INTERVAL_SECONDS = 5 * 60
GOOGLE_AUTH_STATE_TTL_SECONDS = 10 * 60
GOOGLE_AUTH_STATE_FALLBACK_SECRET = secrets.token_bytes(32)
GOOGLE_AUTH_STATE_SECRET_ENV = "ANIME_GOOGLE_AUTH_STATE_SECRET"
GOOGLE_AUTH_STATE_ERROR = "Не удалось подтвердить ответ Google. Попробуйте войти еще раз."
LOGIN_HANDOFF_TTL_SECONDS = 60
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
PLAYER_HOSTS = (
    "kodikplayer.com",
    "aniboom.one",
    "animego.me",
    "sibnet.ru",
    "anivod.com",
    "yummyani.me",
    "aksor.tv",
    "vk.com",
)
PLAYER_FRAME_SOURCES = tuple(
    source
    for host in PLAYER_HOSTS
    for source in (f"https://{host}", f"https://*.{host}")
)


def content_security_policy(script_nonce=None):
    script_sources = ["'self'", "https://accounts.google.com"]
    if script_nonce:
        script_sources.append(f"'nonce-{script_nonce}'")
    return "; ".join(
        (
            "default-src 'self'",
            "base-uri 'self'",
            "object-src 'none'",
            "frame-ancestors 'none'",
            f"script-src {' '.join(script_sources)}",
            "style-src 'self' 'unsafe-inline' https://accounts.google.com",
            "img-src 'self' data: https:",
            "connect-src 'self' https://accounts.google.com",
            f"frame-src 'self' https://accounts.google.com {' '.join(PLAYER_FRAME_SOURCES)}",
            "form-action 'self' https://accounts.google.com",
        )
    )


CONTENT_SECURITY_POLICY = content_security_policy()
EXTERNAL_RATING_SOURCES = {
    "tal": ("TAL", 0),
    "myanimelist": ("MAL", 1),
    "mal": ("MAL", 1),
    "anilist": ("AniList", 2),
    "shikimori": ("Shikimori", 3),
    "imdb": ("IMDB", 4),
}
EXTERNAL_RATING_LABEL_SQL = ", ".join(f"'{key}'" for key in EXTERNAL_RATING_SOURCES)
EXTERNAL_RATING_FIELD_PREDICATE_SQL = (
    "value is not null and "
    f"lower(trim(label)) in ({EXTERNAL_RATING_LABEL_SQL})"
)
EXTERNAL_RATING_INDEX_NAME = "idx_anime_fields_external_rating"
EXTERNAL_RATING_INDEX_SQL = f"""
    create index {EXTERNAL_RATING_INDEX_NAME}
    on anime_fields(lower(trim(label)), anime_id, label, value)
    where {EXTERNAL_RATING_FIELD_PREDICATE_SQL}
"""
SOURCE_PRIORITY = {
    "animego": 0,
    "yummyanime": 1,
}
MERGEABLE_SOURCES = {"animego", "yummyanime"}
PINNED_TRANSLATION_KEYS = {"dream cast"}
GENERIC_TRANSLATION_KEYS = {"", "unknown", "yummyanime"}
SUBTITLE_TRANSLATION_KEYS = {"субтитры", "subtitles"}
TRANSLATION_KEY_ALIASES = {
    "dreamcast": "dream cast",
    "dream cast": "dream cast",
    "light family": "lightfamily",
}
TRANSLATION_PREFIXES = ("озвучка ",)
UNKNOWN_TRANSLATION_RANK = 9999
WATCH_EVENT_TYPES = {
    "player_loaded",
    "player_engaged",
    "heartbeat",
    "fullscreen_enter",
    "pip_open",
    "episode_selected",
    "source_changed",
    "page_hidden",
    "session_end",
}
WATCH_PROGRESS_EVENT_TYPES = {
    "player_engaged",
    "heartbeat",
    "fullscreen_enter",
    "pip_open",
}
WATCH_STARTED_CONFIDENCE = 0.65
MEANINGFUL_WATCH_SECONDS = 5 * 60
WATCH_LIKELY_COMPLETED_SECONDS = 18 * 60
WATCH_NEXT_EPISODE_COMPLETION_SECONDS = 60
MAX_WATCH_EVENT_ENGAGED_SECONDS = 5 * 60
FAVORITE_RECOMMENDATION_SEED_WEIGHT = recommendation_model.FAVORITE_SEED_WEIGHT
WATCHED_RECOMMENDATION_SEED_WEIGHT = recommendation_model.WATCHED_SEED_WEIGHT
MEANINGFUL_WATCH_RECOMMENDATION_SEED_WEIGHT = recommendation_model.meaningful_watch_seed_weight(
    MEANINGFUL_WATCH_SECONDS
)
CATALOG_CACHE = {}
CATALOG_CACHE_LOCK = threading.RLock()
CATALOG_CACHE_BUILDS = {}
DATABASE_INIT_LOCK = threading.RLock()
INITIALIZED_DATABASES = {}
READINESS_CACHE = OrderedDict()
READINESS_CACHE_LOCK = threading.Lock()
DEFAULT_READINESS_CACHE_TTL_SECONDS = 60.0
DEFAULT_READINESS_FAILURE_CACHE_TTL_SECONDS = 2.0
READINESS_CACHE_MAX_ENTRIES = 32
CATALOG_REVISION_TABLES = (
    "anime",
    "episodes",
    "video_sources",
    "anime_fields",
    "anime_genres",
    "anime_dubbings",
    "anime_title_aliases",
    "content_update_events",
)
SEARCH_FOLDS = (
    (re.compile("тсу"), "цу"),
    (re.compile("дж([аеёиоуыэюя])"), r"дз\1"),
    (re.compile("ши"), "си"),
    (re.compile("чи"), "ти"),
    (re.compile("tsu"), "tu"),
    (re.compile("shi"), "si"),
    (re.compile("chi"), "ti"),
    (re.compile("ji"), "zi"),
    (re.compile("ou"), "o"),
    (re.compile("oo"), "o"),
)
MIN_SEARCH_FUZZY_LENGTH = 4
PRIMARY_TITLE_SEARCH_WEIGHT = 14
SUBTITLE_SEARCH_WEIGHT = 11
VARIANT_TITLE_SEARCH_WEIGHT = 10
VARIANT_SUBTITLE_SEARCH_WEIGHT = 9
SOURCE_SEARCH_WEIGHT = 3
GENRE_SEARCH_WEIGHT = 5
TITLE_ALIAS_SEARCH_WEIGHTS = {
    "manual": 9,
    "ru_alt": 9,
    "english": 8,
    "native": 8,
    "romaji": 8,
    "synonym": 7,
    "other_title": 7,
}
SEARCH_METADATA_LABEL_WEIGHTS = {
    "Режиссер": 5,
    "Режиссёр": 5,
    "Автор оригинала": 5,
    "Студия": 4,
    "Франшиза": 4,
    "Жанр": 3,
    "Жанры": 3,
    "Тема": 3,
    "Первоисточник": 2,
}
CURATED_TITLE_ALIASES = (
    {
        "anime_id": 10001570,
        "alias": "Мальчик и херон",
        "language": "ru",
        "alias_type": "manual",
        "source": "curated",
    },
    {
        "anime_id": 10001570,
        "alias": "Мальчик и цапля",
        "language": "ru",
        "alias_type": "ru_alt",
        "source": "curated",
    },
    {
        "anime_id": 10001570,
        "alias": "Мальчик и птица",
        "language": "ru",
        "alias_type": "ru_alt",
        "source": "curated",
    },
    {
        "anime_id": 10001570,
        "alias": "The Boy and the Heron",
        "language": "en",
        "alias_type": "english",
        "source": "curated",
    },
    {
        "anime_id": 10001570,
        "alias": "How Do You Live?",
        "language": "en",
        "alias_type": "synonym",
        "source": "curated",
    },
    {
        "anime_id": 10001570,
        "alias": "君たちはどう生きるか",
        "language": "ja",
        "alias_type": "native",
        "source": "curated",
    },
)
LOGGING_LOCK = threading.RLock()
LOGGING_DIR = None
SERVER_LOGGER_NAME = "anime.server"
CLIENT_ERROR_LOGGER_NAME = "anime.client_errors"
PERFORMANCE_LOGGER_NAME = "anime.performance"
SENSITIVE_CLIENT_KEYS = {
    "authorization",
    "cookie",
    "credential",
    "embed_url",
    "id_token",
    "password",
    "player",
    "secret",
    "src",
    "token",
}
SENSITIVE_CLIENT_PATTERNS = (
    re.compile(r"(?i)\b(authorization:\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)https?://[^\s\"'<>]*(?:alloha|embed|kodik|player|video)[^\s\"'<>]*"),
    re.compile(r"(?i)\b((?:credential|id_token|access_token|refresh_token|token|secret|password)=)([^&\s]+)"),
)
SLUG_TRANSLIT = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "j",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "c",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class ClientErrorPayloadTooLarge(Exception):
    pass


def log_dir():
    return Path(os.environ.get("ANIME_LOG_DIR") or DEFAULT_LOG_DIR).expanduser()


def configure_logging():
    global LOGGING_DIR
    target = log_dir()
    with LOGGING_LOCK:
        if LOGGING_DIR == target:
            return
        target.mkdir(parents=True, exist_ok=True)

        server_logger_obj = logging.getLogger(SERVER_LOGGER_NAME)
        client_logger_obj = logging.getLogger(CLIENT_ERROR_LOGGER_NAME)
        performance_logger_obj = logging.getLogger(PERFORMANCE_LOGGER_NAME)

        for logger_obj in (server_logger_obj, client_logger_obj, performance_logger_obj):
            logger_obj.setLevel(logging.INFO)
            logger_obj.propagate = False
            for handler in list(logger_obj.handlers):
                if getattr(handler, "_anime_log_handler", False):
                    logger_obj.removeHandler(handler)
                    handler.close()

        server_handler = RotatingFileHandler(
            target / "server.log",
            maxBytes=1_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        server_handler._anime_log_handler = True
        server_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        server_logger_obj.addHandler(server_handler)

        client_handler = RotatingFileHandler(
            target / "client-errors.log",
            maxBytes=1_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        client_handler._anime_log_handler = True
        client_handler.setFormatter(logging.Formatter("%(message)s"))
        client_logger_obj.addHandler(client_handler)

        performance_handler = RotatingFileHandler(
            target / "performance.log",
            maxBytes=1_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        performance_handler._anime_log_handler = True
        performance_handler.setFormatter(logging.Formatter("%(message)s"))
        performance_logger_obj.addHandler(performance_handler)

        performance_stream_handler = logging.StreamHandler(sys.stdout)
        performance_stream_handler._anime_log_handler = True
        performance_stream_handler.setFormatter(logging.Formatter("%(message)s"))
        performance_logger_obj.addHandler(performance_stream_handler)
        LOGGING_DIR = target


def server_logger():
    configure_logging()
    return logging.getLogger(SERVER_LOGGER_NAME)


def client_error_logger():
    configure_logging()
    return logging.getLogger(CLIENT_ERROR_LOGGER_NAME)


def performance_logger():
    configure_logging()
    return logging.getLogger(PERFORMANCE_LOGGER_NAME)


def redact_client_text(value):
    text = str(value)
    for pattern in SENSITIVE_CLIENT_PATTERNS:
        text = pattern.sub(lambda match: f"{match.group(1)}<redacted>" if match.groups() else "<redacted-url>", text)
    if len(text) > MAX_CLIENT_ERROR_TEXT:
        return f"{text[:MAX_CLIENT_ERROR_TEXT]}...[truncated]"
    return text


def sanitize_client_error_value(value, depth=0):
    if depth > 4:
        return "[max-depth]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_client_text(value)
    if isinstance(value, dict):
        result = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_CLIENT_ERROR_COLLECTION_ITEMS:
                result["...[truncated]"] = True
                break
            key_text = redact_client_text(key)[:120]
            if key_text.lower() in SENSITIVE_CLIENT_KEYS:
                result[key_text] = "<redacted>"
            else:
                result[key_text] = sanitize_client_error_value(item, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        result = [
            sanitize_client_error_value(item, depth + 1)
            for item in value[:MAX_CLIENT_ERROR_COLLECTION_ITEMS]
        ]
        if len(value) > MAX_CLIENT_ERROR_COLLECTION_ITEMS:
            result.append("[truncated]")
        return result
    return redact_client_text(value)


class AuthError(Exception):
    pass


class AuthConfigError(Exception):
    pass


class ContentSyncPartialError(RuntimeError):
    def __init__(self, event):
        self.event = event
        failed = sum(
            int(source_stats.get("failed") or 0)
            for source_stats in (event.get("stats") or {}).values()
            if isinstance(source_stats, dict)
        )
        super().__init__(f"content sync completed with {failed} failed operation(s)")


class ContentSyncBusyError(RuntimeError):
    pass


def env_list(name):
    return [
        item.strip().lower()
        for item in os.environ.get(name, "").split(",")
        if item.strip()
    ]


def parse_env_value(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_env_file(path):
    path = Path(path)
    if not path.exists():
        return False
    changed = False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = parse_env_value(value)
        changed = True
    return changed


def ensure_auth_schema(con):
    changed = False
    if not con.execute("select 1 from sqlite_master where type = 'table' and name = 'users'").fetchone():
        con.execute(
            """
            create table users (
                id integer primary key autoincrement,
                google_sub text not null unique,
                email text,
                email_verified integer not null default 0,
                name text,
                picture_url text,
                created_at text not null,
                last_login_at text
            )
            """
        )
        changed = True

    if not con.execute("select 1 from sqlite_master where type = 'table' and name = 'sessions'").fetchone():
        con.execute(
            """
            create table sessions (
                token_hash text primary key,
                user_id integer not null,
                created_at text not null,
                expires_at text not null,
                revoked_at text,
                last_seen_at text,
                foreign key (user_id) references users(id) on delete cascade
            )
            """
        )
        changed = True

    handoff_exists = con.execute(
        "select 1 from sqlite_master where type = 'table' and name = 'login_handoffs'"
    ).fetchone()
    if handoff_exists:
        handoff_columns = {
            row[1] for row in con.execute("pragma table_info(login_handoffs)").fetchall()
        }
        expected_handoff_columns = {
            "code_hash",
            "user_id",
            "next_path",
            "created_at",
            "expires_at",
        }
        if handoff_columns != expected_handoff_columns:
            # Handoffs are deliberately short-lived. Dropping an old-format
            # table is safer than retaining a recoverable long-lived token.
            con.execute("drop table login_handoffs")
            handoff_exists = None
            changed = True

    if not handoff_exists:
        con.execute(
            """
            create table login_handoffs (
                code_hash text primary key,
                user_id integer not null,
                next_path text not null,
                created_at text not null,
                expires_at text not null,
                foreign key (user_id) references users(id) on delete cascade
            )
            """
        )
        changed = True

    changed |= ensure_index(
        con,
        "idx_sessions_user_id",
        "create index idx_sessions_user_id on sessions(user_id)",
    )
    changed |= ensure_index(
        con,
        "idx_sessions_expires_at",
        "create index idx_sessions_expires_at on sessions(expires_at)",
    )
    changed |= ensure_index(
        con,
        "idx_login_handoffs_expires_at",
        "create index idx_login_handoffs_expires_at on login_handoffs(expires_at)",
    )

    return changed


def create_user_title_state_table(con):
    con.execute(
        """
        create table user_title_state (
            user_id integer not null,
            anime_id integer not null,
            is_favorite integer not null default 0,
            progress_episode_number integer,
            watched integer not null default 0,
            watch_status text not null default 'none',
            not_interested integer not null default 0,
            updated_at text not null,
            favorite_updated_at text,
            watch_status_updated_at text,
            not_interested_updated_at text,
            primary key (user_id, anime_id),
            foreign key (user_id) references users(id) on delete cascade,
            foreign key (anime_id) references anime(id) on delete cascade
        )
        """
    )


def user_title_state_needs_rebuild(con):
    rows = con.execute("pragma table_info(user_title_state)").fetchall()
    if not rows:
        return False
    columns = {row[1] for row in rows}
    pk_columns = [row[1] for row in sorted((row for row in rows if row[5]), key=lambda row: row[5])]
    return "user_id" not in columns or pk_columns != ["user_id", "anime_id"]


def ensure_user_state_schema(con):
    exists = con.execute(
        "select 1 from sqlite_master where type = 'table' and name = 'user_title_state'"
    ).fetchone()
    if not exists:
        create_user_title_state_table(con)
        ensure_user_state_indexes(con)
        return True

    if not user_title_state_needs_rebuild(con):
        changed = ensure_columns(
            con,
            "user_title_state",
            {
                "watch_status": "text not null default 'none'",
                "not_interested": "integer not null default 0",
                "favorite_updated_at": "text",
                "watch_status_updated_at": "text",
                "not_interested_updated_at": "text",
            },
        )
        if changed:
            con.execute(
                """
                update user_title_state
                set watch_status = case
                        when watched = 1 or watch_status = 'completed' then 'completed'
                        when watch_status in ('planned', 'dropped') then 'none'
                        when watch_status in ('watching', 'paused') then 'watching'
                        when progress_episode_number is not null then 'watching'
                        else 'none'
                    end,
                    watched = case
                        when watched = 1 or watch_status = 'completed' then 1
                        else 0
                    end,
                    progress_episode_number = case
                        when watch_status in ('planned', 'dropped') then null
                        else progress_episode_number
                    end,
                    not_interested = case when is_favorite = 1 then 0 else not_interested end,
                    favorite_updated_at = case when is_favorite = 1 then updated_at else null end,
                    watch_status_updated_at = case
                        when watched = 1 or progress_episode_number is not null then updated_at
                        else null
                    end
                """
            )
        changed |= normalize_user_title_state_rows(con)
        return ensure_user_state_indexes(con) or changed

    old_columns = {row[1] for row in con.execute("pragma table_info(user_title_state)").fetchall()}
    con.execute("alter table user_title_state rename to user_title_state_old")
    create_user_title_state_table(con)

    if "user_id" in old_columns:
        def existing_value(column, fallback):
            return column if column in old_columns else fallback

        con.execute(
            f"""
            insert or replace into user_title_state (
                user_id,
                anime_id,
                is_favorite,
                progress_episode_number,
                watched,
                watch_status,
                not_interested,
                updated_at,
                favorite_updated_at,
                watch_status_updated_at,
                not_interested_updated_at
            )
            select
                user_id,
                anime_id,
                coalesce(is_favorite, 0),
                progress_episode_number,
                coalesce(watched, 0),
                {existing_value("watch_status", "case when watched = 1 then 'completed' when progress_episode_number is not null then 'watching' else 'none' end")},
                coalesce({existing_value("not_interested", "0")}, 0),
                coalesce(updated_at, ?),
                {existing_value("favorite_updated_at", "case when is_favorite = 1 then updated_at else null end")},
                {existing_value("watch_status_updated_at", "case when watched = 1 or progress_episode_number is not null then updated_at else null end")},
                {existing_value("not_interested_updated_at", "null")}
            from user_title_state_old
            where user_id is not null
              and anime_id is not null
              and exists (
                  select 1
                  from users u
                  where u.id = user_title_state_old.user_id
              )
            """,
            (now_iso(),),
        )
    con.execute("drop table user_title_state_old")
    normalize_user_title_state_rows(con)
    ensure_user_state_indexes(con)
    return True


def normalize_user_title_state_rows(con):
    before = con.total_changes
    con.execute(
        """
        update user_title_state
        set watch_status = case
                when coalesce(watched, 0) = 1 or watch_status = 'completed' then 'completed'
                when watch_status in ('watching', 'paused') then 'watching'
                when watch_status is null and progress_episode_number is not null then 'watching'
                else 'none'
            end,
            watched = case
                when coalesce(watched, 0) = 1 or watch_status = 'completed' then 1
                else 0
            end,
            progress_episode_number = case
                when coalesce(watched, 0) = 0
                 and coalesce(watch_status, '') not in ('completed', 'watching', 'paused')
                 and not (watch_status is null and progress_episode_number is not null)
                then null
                else progress_episode_number
            end,
            not_interested = case
                when coalesce(is_favorite, 0) = 1 then 0
                else coalesce(not_interested, 0)
            end
        where watch_status is null
           or watch_status not in ('none', 'watching', 'completed')
           or (coalesce(watched, 0) = 1 and watch_status != 'completed')
           or (watch_status = 'completed' and coalesce(watched, 0) != 1)
           or (watch_status in ('none', 'watching') and coalesce(watched, 0) != 0)
           or (watch_status = 'none' and progress_episode_number is not null)
           or (coalesce(is_favorite, 0) = 1 and coalesce(not_interested, 0) = 1)
        """
    )
    return con.total_changes > before


def ensure_user_state_indexes(con):
    changed = ensure_index(
        con,
        "idx_user_title_state_user_watch_status",
        """
        create index idx_user_title_state_user_watch_status
        on user_title_state(user_id, watch_status, watch_status_updated_at desc)
        """,
    )
    changed |= ensure_index(
        con,
        "idx_user_title_state_user_not_interested",
        """
        create index idx_user_title_state_user_not_interested
        on user_title_state(user_id, not_interested)
        where not_interested = 1
        """,
    )
    return changed


def ensure_watch_tracking_schema(con):
    had_events = con.execute(
        "select 1 from sqlite_master where type = 'table' and name = 'user_watch_events'"
    ).fetchone()
    had_state = con.execute(
        "select 1 from sqlite_master where type = 'table' and name = 'user_episode_state'"
    ).fetchone()
    con.executescript(
        """
        create table if not exists user_watch_events (
            id integer primary key autoincrement,
            user_id integer not null references users(id) on delete cascade,
            anime_id integer not null references anime(id) on delete cascade,
            episode_id integer references episodes(id) on delete set null,
            video_source_id integer references video_sources(id) on delete set null,
            client_session_id text not null,
            event_type text not null,
            event_at text not null,
            episode_number text,
            progress_episode_number integer,
            source text,
            source_anime_id integer references anime(id) on delete set null,
            translation_id text,
            translation_title text,
            provider_id text,
            provider_title text,
            embed_host text,
            engaged_seconds integer not null default 0,
            page_visible integer,
            player_focused integer,
            confidence real not null default 0,
            metadata_json text not null default '{}',
            created_at text not null,
            check (event_type in (
                'player_loaded',
                'player_engaged',
                'heartbeat',
                'fullscreen_enter',
                'pip_open',
                'episode_selected',
                'source_changed',
                'page_hidden',
                'session_end'
            ))
        );

        create table if not exists user_episode_state (
            user_id integer not null references users(id) on delete cascade,
            anime_id integer not null references anime(id) on delete cascade,
            episode_id integer not null references episodes(id) on delete cascade,
            episode_number text,
            progress_episode_number integer,
            video_source_id integer references video_sources(id) on delete set null,
            source text,
            source_anime_id integer references anime(id) on delete set null,
            translation_id text,
            translation_title text,
            provider_id text,
            provider_title text,
            embed_host text,
            first_seen_at text not null,
            last_seen_at text not null,
            started_at text,
            completed_at text,
            engaged_seconds integer not null default 0,
            heartbeat_count integer not null default 0,
            last_event_type text not null,
            last_confidence real not null default 0,
            completion_confidence real,
            updated_at text not null,
            primary key (user_id, anime_id, episode_id)
        );

        create index if not exists idx_user_watch_events_user_at
            on user_watch_events(user_id, event_at desc);
        create index if not exists idx_user_watch_events_session
            on user_watch_events(user_id, client_session_id, event_at);
        create index if not exists idx_user_watch_events_episode
            on user_watch_events(user_id, anime_id, episode_id, event_at desc);
        create index if not exists idx_user_episode_state_user_seen
            on user_episode_state(user_id, last_seen_at desc);
        create index if not exists idx_user_episode_state_anime_progress
            on user_episode_state(user_id, anime_id, progress_episode_number);
        """
    )
    return not bool(had_events and had_state)


def ensure_title_navigation_schema(con):
    existed = con.execute(
        "select 1 from sqlite_master where type = 'table' and name = 'user_title_navigation_state'"
    ).fetchone()
    con.executescript(
        """
        create table if not exists user_title_navigation_state (
            user_id integer not null references users(id) on delete cascade,
            anime_id integer not null references anime(id) on delete cascade,
            episode_id integer references episodes(id) on delete set null,
            episode_number text,
            updated_at text not null,
            primary key (user_id, anime_id)
        );

        create index if not exists idx_user_title_navigation_user_updated
            on user_title_navigation_state(user_id, updated_at desc);
        """
    )
    return not bool(existed)


def purge_orphaned_user_data(con):
    if not con.execute("select 1 from sqlite_master where type = 'table' and name = 'users'").fetchone():
        return False

    before = con.total_changes
    table_names = {
        row[0]
        for row in con.execute("select name from sqlite_master where type = 'table'").fetchall()
    }

    if "sessions" in table_names:
        con.execute(
            """
            delete from sessions
            where not exists (select 1 from users where users.id = sessions.user_id)
            """
        )

    if "user_title_state" in table_names:
        state_columns = {row[1] for row in con.execute("pragma table_info(user_title_state)").fetchall()}
        if "user_id" in state_columns:
            con.execute(
                """
                delete from user_title_state
                where not exists (select 1 from users where users.id = user_title_state.user_id)
                   or not exists (select 1 from anime where anime.id = user_title_state.anime_id)
                """
            )

    if "user_title_navigation_state" in table_names:
        con.execute(
            """
            delete from user_title_navigation_state
            where not exists (select 1 from users where users.id = user_title_navigation_state.user_id)
               or not exists (select 1 from anime where anime.id = user_title_navigation_state.anime_id)
            """
        )
        con.execute(
            """
            update user_title_navigation_state
            set episode_id = null
            where episode_id is not null
              and not exists (select 1 from episodes where episodes.id = user_title_navigation_state.episode_id)
            """
        )

    if "user_watch_events" in table_names:
        con.execute(
            """
            delete from user_watch_events
            where not exists (select 1 from users where users.id = user_watch_events.user_id)
               or not exists (select 1 from anime where anime.id = user_watch_events.anime_id)
            """
        )
        con.execute(
            """
            update user_watch_events
            set episode_id = null
            where episode_id is not null
              and not exists (select 1 from episodes where episodes.id = user_watch_events.episode_id)
            """
        )
        con.execute(
            """
            update user_watch_events
            set video_source_id = null
            where video_source_id is not null
              and not exists (
                  select 1 from video_sources where video_sources.id = user_watch_events.video_source_id
              )
            """
        )
        con.execute(
            """
            update user_watch_events
            set source_anime_id = null
            where source_anime_id is not null
              and not exists (select 1 from anime where anime.id = user_watch_events.source_anime_id)
            """
        )

    if "user_episode_state" in table_names:
        con.execute(
            """
            delete from user_episode_state
            where not exists (select 1 from users where users.id = user_episode_state.user_id)
               or not exists (select 1 from anime where anime.id = user_episode_state.anime_id)
               or not exists (select 1 from episodes where episodes.id = user_episode_state.episode_id)
            """
        )
        con.execute(
            """
            update user_episode_state
            set video_source_id = null
            where video_source_id is not null
              and not exists (
                  select 1 from video_sources where video_sources.id = user_episode_state.video_source_id
              )
            """
        )
        con.execute(
            """
            update user_episode_state
            set source_anime_id = null
            where source_anime_id is not null
              and not exists (select 1 from anime where anime.id = user_episode_state.source_anime_id)
            """
        )
    return con.total_changes != before


def ensure_columns(con, table, columns):
    existing = {row[1] for row in con.execute(f"pragma table_info({table})")}
    changed = False
    for column, definition in columns.items():
        if column not in existing:
            con.execute(f"alter table {table} add column {column} {definition}")
            changed = True
    return changed


def clear_inactive_episode_starts(con):
    before = con.total_changes
    con.execute(
        """
        update user_episode_state
        set started_at = null,
            last_event_type = 'manual_clear',
            last_confidence = 1.0,
            updated_at = coalesce(
                (
                    select coalesce(uts.watch_status_updated_at, uts.updated_at)
                    from user_title_state uts
                    where uts.user_id = user_episode_state.user_id
                      and uts.anime_id = user_episode_state.anime_id
                ),
                updated_at
            )
        where started_at is not null
          and exists (
              select 1
              from user_title_state uts
              where uts.user_id = user_episode_state.user_id
                and uts.anime_id = user_episode_state.anime_id
                and uts.watch_status = 'none'
          )
        """
    )
    return con.total_changes > before


def ensure_index(con, name, sql):
    exists = con.execute(
        "select 1 from sqlite_master where type = 'index' and name = ?",
        (name,),
    ).fetchone()
    if exists:
        return False
    con.execute(sql)
    return True


def ensure_title_alias_schema(con):
    changed = False
    if not con.execute(
        "select 1 from sqlite_master where type = 'table' and name = 'anime_title_aliases'"
    ).fetchone():
        con.execute(
            """
            create table anime_title_aliases (
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
            )
            """
        )
        changed = True
    changed |= ensure_index(
        con,
        "idx_anime_title_aliases_anime_id",
        "create index idx_anime_title_aliases_anime_id on anime_title_aliases(anime_id)",
    )
    changed |= ensure_index(
        con,
        "idx_anime_title_aliases_normalized",
        "create index idx_anime_title_aliases_normalized on anime_title_aliases(normalized_alias)",
    )
    return changed


def ensure_curated_title_aliases(con):
    before = con.total_changes
    timestamp = now_iso()
    for alias in CURATED_TITLE_ALIASES:
        anime_id = alias["anime_id"]
        if not con.execute("select 1 from anime where id = ?", (anime_id,)).fetchone():
            continue
        normalized_alias = normalize_search_text(alias["alias"])
        if not normalized_alias:
            continue
        con.execute(
            """
            insert into anime_title_aliases (
                anime_id, alias, normalized_alias, language, alias_type, source,
                source_ref, confidence, created_at, updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(anime_id, normalized_alias, source, alias_type) do update set
                alias = excluded.alias,
                language = excluded.language,
                source_ref = excluded.source_ref,
                confidence = excluded.confidence,
                updated_at = excluded.updated_at
            where anime_title_aliases.alias is not excluded.alias
               or anime_title_aliases.language is not excluded.language
               or anime_title_aliases.source_ref is not excluded.source_ref
               or anime_title_aliases.confidence is not excluded.confidence
            """,
            (
                anime_id,
                alias["alias"],
                normalized_alias,
                alias.get("language"),
                alias.get("alias_type") or "alias",
                alias.get("source") or "manual",
                alias.get("source_ref"),
                alias.get("confidence", 1.0),
                timestamp,
                timestamp,
            ),
        )
    return con.total_changes != before


def ensure_catalog_schema(con):
    changed = ensure_title_alias_schema(con)
    changed |= ensure_columns(
        con,
        "anime",
        {
            "source": "text",
            "source_id": "text",
        },
    )
    if con.execute("select 1 from anime where source is null limit 1").fetchone():
        con.execute("update anime set source = 'animego' where source is null")
        changed = True
    if con.execute("select 1 from anime where source_id is null limit 1").fetchone():
        con.execute("update anime set source_id = cast(id as text) where source_id is null")
        changed = True
    changed |= ensure_curated_title_aliases(con)
    return changed


def ensure_runtime_indexes(con):
    had_content_updates = con.execute(
        "select 1 from sqlite_master where type = 'table' and name = 'content_update_events'"
    ).fetchone()
    content_updates.ensure_schema(con)
    changed = not bool(had_content_updates)
    changed |= ensure_index(
        con,
        "idx_episodes_anime_id",
        "create index idx_episodes_anime_id on episodes(anime_id)",
    )
    changed |= ensure_index(
        con,
        "idx_video_sources_anime_embed",
        "create index idx_video_sources_anime_embed on video_sources(anime_id, embed_url)",
    )
    changed |= ensure_index(
        con,
        "idx_video_sources_episode_embed",
        "create index idx_video_sources_episode_embed on video_sources(episode_id, embed_url)",
    )
    changed |= ensure_index(
        con,
        EXTERNAL_RATING_INDEX_NAME,
        EXTERNAL_RATING_INDEX_SQL,
    )
    return changed


def catalog_revision_trigger_name(table, operation):
    return f"catalog_revision_{table}_{operation}"


def ensure_catalog_revision_schema(con):
    """Install a cheap, durable catalog-dirty marker.

    The first catalog row changed after a cache snapshot flips the marker and
    increments its generation. Further rows in the same scrape only execute a
    primary-key lookup, avoiding a revision-row write for every scraped row.
    User/session tables have no triggers, so their writes never invalidate the
    immutable catalog cache and cannot race with an acknowledgement step.
    """
    changed = False
    if not con.execute(
        "select 1 from sqlite_master where type = 'table' and name = 'catalog_cache_revision'"
    ).fetchone():
        con.execute(
            """
            create table catalog_cache_revision (
                singleton integer primary key check (singleton = 1),
                generation integer not null default 0,
                dirty integer not null default 1 check (dirty in (0, 1))
            )
            """
        )
        changed = True
    before = con.total_changes
    con.execute(
        """
        insert or ignore into catalog_cache_revision(singleton, generation, dirty)
        values (1, 0, 1)
        """
    )
    changed |= con.total_changes != before

    existing_tables = {
        row[0]
        for row in con.execute("select name from sqlite_master where type = 'table'").fetchall()
    }
    existing_triggers = {
        row[0]
        for row in con.execute("select name from sqlite_master where type = 'trigger'").fetchall()
    }
    for table in CATALOG_REVISION_TABLES:
        if table not in existing_tables:
            continue
        for operation in ("insert", "update", "delete"):
            trigger_name = catalog_revision_trigger_name(table, operation)
            if trigger_name in existing_triggers:
                continue
            con.execute(
                f"""
                create trigger {trigger_name}
                after {operation} on {table}
                begin
                    update catalog_cache_revision
                    set generation = generation + 1,
                        dirty = 1
                    where singleton = 1 and dirty = 0;
                end
                """
            )
            changed = True
    return changed


def resolve_db_path(db_path=None):
    return Path(db_path or os.environ.get("ANIMEGO_DB") or DEFAULT_DB)


def truthy_env(name):
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def catalog_prewarm_enabled():
    return os.environ.get("ANIME_PREWARM_CATALOG", "1").strip().lower() not in {"0", "false", "no", "off"}


USER_LIBRARY_MIGRATION_PATH = (
    "2026-07-09_zzzzz-user-library-state/00_add_user_library_state.sql"
)


def maybe_apply_database_migrations(path):
    if not truthy_env("ANIME_AUTO_MIGRATE"):
        return False

    from scripts import db_migrate

    roots_env = os.environ.get("ANIME_MIGRATIONS_ROOTS")
    if roots_env:
        roots = [Path(part) for part in roots_env.split(os.pathsep) if part]
    else:
        roots = [Path(os.environ.get("ANIME_MIGRATIONS_ROOT") or ROOT / "migrations")]
    backup_dir = os.environ.get("ANIME_MIGRATION_BACKUP_DIR")
    result = db_migrate.apply_pending(
        path,
        roots,
        backup_dir=backup_dir,
        no_backup=truthy_env("ANIME_MIGRATION_NO_BACKUP"),
        wait_lock=True,
    )
    for migration, duration_ms in result["applied"]:
        print(f"Applied database migration {migration.path} ({duration_ms} ms)")
    for migration in result.get("adopted", []):
        print(f"Adopted runtime-satisfied database migration {migration.path}")
    return bool(result["applied"] or result.get("adopted"))


def ensure_base_database(path):
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        has_catalog_schema = con.execute(
            "select 1 from sqlite_master where type = 'table' and name = 'anime'"
        ).fetchone()
    finally:
        con.close()
    if not has_catalog_schema:
        import scrape_animego

        con = scrape_animego.init_db(path)
        con.close()


def database_file_identity(path, con):
    stat = Path(path).stat()
    schema_version = int(con.execute("pragma schema_version").fetchone()[0])
    return stat.st_dev, stat.st_ino, schema_version


def configure_connection(con):
    con.row_factory = sqlite3.Row
    con.execute("pragma busy_timeout=30000")
    con.execute("pragma foreign_keys=on")
    if con.execute("pragma foreign_keys").fetchone()[0] != 1:
        con.close()
        raise RuntimeError("SQLite foreign key enforcement could not be enabled")
    return con


def open_configured_connection(path):
    con = sqlite3.connect(path)
    try:
        return configure_connection(con)
    except Exception:
        con.close()
        raise


def initialize_database(path):
    ensure_base_database(path)
    con = sqlite3.connect(path)
    configure_connection(con)
    try:
        changed = ensure_catalog_schema(con)
        changed |= ensure_auth_schema(con)
        changed |= ensure_user_state_schema(con)
        changed |= ensure_watch_tracking_schema(con)
        changed |= ensure_title_navigation_schema(con)
        changed |= clear_inactive_episode_starts(con)
        changed |= purge_orphaned_user_data(con)
        changed |= ensure_runtime_indexes(con)
        changed |= ensure_catalog_revision_schema(con)
        if changed:
            con.commit()
        else:
            con.rollback()
    finally:
        con.close()


def connect(db_path=None):
    path = resolve_db_path(db_path)
    key = str(path.resolve())
    if path.is_file():
        con = open_configured_connection(path)
        try:
            identity = database_file_identity(path, con)
        except Exception:
            con.close()
            raise
        if INITIALIZED_DATABASES.get(key) == identity:
            return con
        con.close()

    with DATABASE_INIT_LOCK:
        if path.is_file():
            con = open_configured_connection(path)
            try:
                identity = database_file_identity(path, con)
            except Exception:
                con.close()
                raise
            if INITIALIZED_DATABASES.get(key) == identity:
                return con
            con.close()

        initialize_database(path)
        con = open_configured_connection(path)
        try:
            INITIALIZED_DATABASES[key] = database_file_identity(path, con)
            return con
        except Exception:
            con.close()
            raise


def reset_database_initialization(db_path=None):
    path = resolve_db_path(db_path)
    with DATABASE_INIT_LOCK:
        INITIALIZED_DATABASES.pop(str(path.resolve()), None)
    with READINESS_CACHE_LOCK:
        READINESS_CACHE.pop(str(path.resolve()), None)


def prepare_database(db_path=None):
    path = resolve_db_path(db_path)
    ensure_base_database(path)
    # Runtime compatibility schema comes first.  In particular, a fresh base
    # catalog has no user_title_state table yet, while the tracked library-state
    # migration is an ALTER migration.  initialize_database creates/upgrades
    # the table and maybe_apply_database_migrations safely adopts that exact
    # migration before applying the rest of the pending plan.
    con = connect(path)
    try:
        con.commit()
    finally:
        con.close()
    if maybe_apply_database_migrations(path):
        reset_database_initialization(path)
        invalidate_catalog_cache(path)
    con = connect(path)
    try:
        con.commit()
    finally:
        con.close()
    return path


def check_database_readiness(path):
    try:
        con = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=2)
        try:
            con.execute("pragma query_only=on")
            quick_check = con.execute("pragma quick_check(1)").fetchone()
            if not quick_check or quick_check[0] != "ok":
                return False
            if con.execute("pragma foreign_key_check").fetchone() is not None:
                return False
            required_columns = {
                "anime": {"id", "slug", "title", "source", "source_id"},
                "episodes": {"id", "anime_id", "number", "has_video"},
                "video_sources": {"id", "anime_id", "episode_id", "embed_url"},
                "users": {"id", "google_sub"},
                "sessions": {"token_hash", "user_id", "expires_at", "revoked_at"},
                "login_handoffs": {"code_hash", "user_id", "expires_at"},
                "user_title_state": {
                    "user_id",
                    "anime_id",
                    "watched",
                    "watch_status",
                    "not_interested",
                    "favorite_updated_at",
                    "watch_status_updated_at",
                    "not_interested_updated_at",
                },
                "user_watch_events": {"id", "user_id", "anime_id", "event_type"},
                "user_episode_state": {"user_id", "anime_id", "episode_id"},
                "user_title_navigation_state": {
                    "user_id",
                    "anime_id",
                    "episode_id",
                    "episode_number",
                    "updated_at",
                },
                "anime_title_aliases": {"anime_id", "normalized_alias"},
                "content_update_events": {"id", "anime_id", "occurred_at"},
                "catalog_cache_revision": {"singleton", "generation", "dirty"},
            }
            existing = {
                row[0]
                for row in con.execute(
                    "select name from sqlite_master where type = 'table'"
                ).fetchall()
            }
            if not required_columns.keys() <= existing:
                return False
            for table, required in required_columns.items():
                columns = {row[1] for row in con.execute(f"pragma table_info({table})")}
                if not required <= columns:
                    return False
                con.execute(f"select 1 from {table} limit 1").fetchone()
            return True
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return False


def bounded_env_seconds(name, default, minimum=0.25, maximum=300.0):
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = float(default)
    if not math.isfinite(value):
        value = float(default)
    return max(minimum, min(maximum, value))


def readiness_cache_ttl(ready):
    if ready:
        return bounded_env_seconds(
            "ANIME_READINESS_CACHE_TTL_SECONDS",
            DEFAULT_READINESS_CACHE_TTL_SECONDS,
        )
    return bounded_env_seconds(
        "ANIME_READINESS_FAILURE_CACHE_TTL_SECONDS",
        DEFAULT_READINESS_FAILURE_CACHE_TTL_SECONDS,
    )


def readiness_file_identity(path):
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_dev, stat.st_ino


def database_is_ready(db_path=None):
    path = resolve_db_path(db_path).resolve()
    key = str(path)
    now = time.monotonic()
    identity = readiness_file_identity(path)
    with READINESS_CACHE_LOCK:
        cached = READINESS_CACHE.get(key)
        if cached and cached[0] > now and cached[2] == identity:
            READINESS_CACHE.move_to_end(key)
            return cached[1]

    ready = identity is not None and path.is_file() and check_database_readiness(path)
    with READINESS_CACHE_LOCK:
        READINESS_CACHE[key] = (now + readiness_cache_ttl(ready), ready, identity)
        READINESS_CACHE.move_to_end(key)
        while len(READINESS_CACHE) > READINESS_CACHE_MAX_ENTRIES:
            READINESS_CACHE.popitem(last=False)
    return ready


def rows_to_dicts(rows):
    return [dict(row) for row in rows]


def normalize_state(row=None):
    return user_state_model.normalized_state(row)


def apply_state_fields(item):
    state = normalize_state(
        {
            **item,
            "updated_at": item.get("state_updated_at"),
        }
    )
    item.update({key: value for key, value in state.items() if key != "updated_at"})
    item["state_updated_at"] = state["updated_at"]
    return item


def resolved_user_id(con, user_id=None):
    return int(user_id) if user_id is not None else None


def require_user_id(con, user_id):
    resolved = resolved_user_id(con, user_id)
    if resolved is None:
        raise ValueError("user_id is required")
    exists = con.execute("select 1 from users where id = ?", (resolved,)).fetchone()
    if not exists:
        raise ValueError("user_id does not exist")
    return resolved


def numeric(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_rating_number(value):
    text = str(value or "").replace(",", ".")
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None
    score = numeric(match.group(0))
    if score is None or score <= 0 or score > 10:
        return None
    return score


def clamp(value, low=0.0, high=1.0):
    return max(low, min(high, value))


@lru_cache(maxsize=200_000)
def normalize_key_text(value):
    text = value.strip().casefold().replace("ё", "е").replace("э", "е")
    return "".join(char for char in unicodedata.normalize("NFKD", text) if not unicodedata.combining(char))


def normalize_match_title(value):
    return normalize_match_title_text(str(value or ""))


@lru_cache(maxsize=200_000)
def normalize_match_title_text(value):
    text = normalize_key(value)
    text = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def fold_search_text(value):
    text = value
    for pattern, replacement in SEARCH_FOLDS:
        text = pattern.sub(replacement, text)
    return text


def normalize_search_text(value):
    return normalize_search_text_text(str(value or ""))


def normalize_key(value):
    return normalize_key_text(str(value or ""))


@lru_cache(maxsize=200_000)
def normalize_search_text_text(value):
    return fold_search_text(normalize_match_title(value))


def search_tokens(value):
    return [token for token in normalize_search_text(value).split() if token]


def unique_search_tokens(tokens):
    return list(dict.fromkeys(tokens))


def search_query_info(value):
    raw = str(value or "")[:MAX_SEARCH_QUERY_CHARS]
    text = normalize_search_text(raw)
    tokens = unique_search_tokens(token for token in text.split() if token)[:MAX_SEARCH_QUERY_TOKENS]
    return {
        "text": " ".join(tokens),
        "tokens": tokens,
    }


def add_search_field(fields, value, weight):
    text = normalize_search_text(value)
    if not text:
        return
    fields.append({
        "text": text,
        "tokens": unique_search_tokens(token for token in text.split() if token),
        "weight": weight,
    })


def split_search_values(value):
    text = str(value or "").strip()
    if not text:
        return []
    parts = [part.strip() for part in re.split(r"\s*(?:,|;|、|，)\s*", text) if part.strip()]
    values = [text]
    if len(parts) > 1:
        values.extend(parts)
    return unique_values(values, lambda item: item)


def make_search_field(value, weight, kind, label=None, source=None):
    if normalize_search_text(value) == "":
        return None
    field = {
        "value": str(value).strip(),
        "weight": int(weight),
        "kind": kind,
    }
    if label:
        field["label"] = label
    if source:
        field["source"] = source
    return field


def unique_structured_search_fields(fields):
    by_key = {}
    for field in fields or []:
        if not isinstance(field, dict):
            field = make_search_field(field, 1, "extra")
        if not field:
            continue
        text = normalize_search_text(field.get("value"))
        if not text:
            continue
        weight = int(numeric(field.get("weight")) or 1)
        cleaned = {
            key: value
            for key, value in field.items()
            if key in {"value", "weight", "kind", "label", "source"} and value not in (None, "")
        }
        cleaned["weight"] = weight
        cleaned.setdefault("kind", "extra")
        key = (text, cleaned.get("kind") or "")
        existing = by_key.get(key)
        if existing is None or cleaned["weight"] > existing["weight"]:
            by_key[key] = cleaned
    return list(by_key.values())


def add_structured_search_fields(fields, search_fields):
    for search_field in search_fields or []:
        if isinstance(search_field, dict):
            add_search_field(fields, search_field.get("value"), int(numeric(search_field.get("weight")) or 1))
        else:
            add_search_field(fields, search_field, 1)


def item_search_fields(item):
    fields = []
    add_search_field(fields, item.get("title"), PRIMARY_TITLE_SEARCH_WEIGHT)
    add_search_field(fields, item.get("subtitle"), SUBTITLE_SEARCH_WEIGHT)
    for variant in item.get("source_variants") or []:
        add_search_field(fields, variant.get("title"), VARIANT_TITLE_SEARCH_WEIGHT)
        add_search_field(fields, variant.get("subtitle"), VARIANT_SUBTITLE_SEARCH_WEIGHT)
        add_search_field(fields, variant.get("source"), SOURCE_SEARCH_WEIGHT)
    for genre in item.get("genres") or []:
        add_search_field(fields, genre, GENRE_SEARCH_WEIGHT)
    add_search_field(fields, item.get("kind"), 3)
    add_search_field(fields, item.get("status"), 3)
    add_search_field(fields, item.get("year"), 2)
    add_search_field(fields, item.get("source"), SOURCE_SEARCH_WEIGHT)
    for source in item.get("sources") or []:
        add_search_field(fields, source, SOURCE_SEARCH_WEIGHT)
    add_structured_search_fields(fields, item.get("search_fields"))
    return fields


def item_search_index(item):
    fields = item_search_fields(item)
    token_weights = {}
    for field in fields:
        for token in field["tokens"]:
            token_weights[token] = max(token_weights.get(token, 0), field["weight"])
    return {
        "fields": fields,
        "tokens": [{"token": token, "weight": weight} for token, weight in token_weights.items()],
    }


def max_search_edit_distance(token):
    if len(token) < MIN_SEARCH_FUZZY_LENGTH:
        return 0
    return 2 if len(token) >= 10 else 1


def bounded_damerau_levenshtein(left, right, max_distance):
    if left == right:
        return 0
    if abs(len(left) - len(right)) > max_distance:
        return max_distance + 1

    previous = list(range(len(right) + 1))
    before_previous = None
    for i, left_char in enumerate(left, 1):
        current = [i]
        row_min = current[0]
        for j, right_char in enumerate(right, 1):
            cost = 0 if left_char == right_char else 1
            value = min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + cost,
            )
            if (
                before_previous is not None
                and i > 1
                and j > 1
                and left[i - 1] == right[j - 2]
                and left[i - 2] == right[j - 1]
            ):
                value = min(value, before_previous[j - 2] + 1)
            current.append(value)
            row_min = min(row_min, value)
        if row_min > max_distance:
            return max_distance + 1
        before_previous = previous
        previous = current
    return previous[-1]


def search_token_match_score(query_token, candidate):
    token = candidate["token"]
    weight = candidate["weight"]
    if token == query_token:
        return 120 + weight * 12 + len(token)

    if len(token) >= 3 and len(query_token) >= 3:
        if token.startswith(query_token):
            return 92 + weight * 10 + len(query_token)
        if (
            query_token.startswith(token)
            and len(token) >= MIN_SEARCH_FUZZY_LENGTH
            and len(token) / len(query_token) >= 0.6
        ):
            return 78 + weight * 8 + len(token)
        if query_token in token:
            return 66 + weight * 7
        if (
            token in query_token
            and len(token) >= MIN_SEARCH_FUZZY_LENGTH
            and len(token) / len(query_token) >= 0.6
        ):
            return 66 + weight * 7

    max_distance = min(max_search_edit_distance(query_token), max_search_edit_distance(token))
    if not max_distance:
        return 0
    distance = bounded_damerau_levenshtein(query_token, token, max_distance)
    if distance <= max_distance:
        return 48 + weight * 6 + min(len(query_token), len(token)) - distance * 10
    return 0


def best_search_token_score(query_token, candidates):
    return max((search_token_match_score(query_token, candidate) for candidate in candidates), default=0)


def search_phrase_score(query, field):
    if not query["text"] or not field["text"]:
        return 0
    if field["text"] == query["text"]:
        return 500 + field["weight"] * 30
    if query["text"] in field["text"]:
        return 360 + field["weight"] * 24 + len(query["text"])
    if field["text"] in query["text"] and len(field["text"]) >= 4:
        return 240 + field["weight"] * 12
    return 0


def required_search_token_matches(count):
    if count <= 2:
        return count
    return math.ceil(count * 0.67)


def item_search_score(item, query):
    return search_index_score(item_search_index(item), query)


def search_index_score(index, query):
    if not query.get("tokens"):
        return 0

    phrase = max((search_phrase_score(query, field) for field in index["fields"]), default=0)
    matched = 0
    token_score = 0
    for token in query["tokens"]:
        score = best_search_token_score(token, index["tokens"])
        if score > 0:
            matched += 1
            token_score += score

    if matched < required_search_token_matches(len(query["tokens"])):
        return phrase
    coverage = matched / len(query["tokens"])
    return phrase + token_score * coverage + matched * 20


def catalog_search_indexes(cache, db_path=None):
    indexes = cache.get("search_indexes")
    if indexes is not None:
        return indexes

    search_fields = catalog_search_fields(cache, db_path)
    built = {
        item["id"]: item_search_index(catalog_item_with_search_fields(item, search_fields.get(item["id"]) or []))
        for item in cache["items"]
    }
    with CATALOG_CACHE_LOCK:
        return cache.setdefault("search_indexes", built)


def external_rating_source(label):
    return EXTERNAL_RATING_SOURCES.get(normalize_match_title(label))


def translation_key(value):
    key = normalize_match_title(value)
    for prefix in TRANSLATION_PREFIXES:
        if key.startswith(prefix):
            key = key[len(prefix):].strip()
            break
    compact = key.replace(" ", "")
    return TRANSLATION_KEY_ALIASES.get(key) or TRANSLATION_KEY_ALIASES.get(compact) or key


def base36(value):
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    number = int(value or 0)
    if number == 0:
        return "0"
    result = []
    while number:
        number, remainder = divmod(number, 36)
        result.append(digits[remainder])
    return "".join(reversed(result))


def slugify_text(value, max_length=72):
    result = []
    previous_dash = False
    for char in str(value or "").casefold():
        if char.isascii() and char.isalnum():
            result.append(char)
            previous_dash = False
            continue
        mapped = SLUG_TRANSLIT.get(char)
        if mapped is not None:
            if mapped:
                result.append(mapped)
                previous_dash = False
            continue
        if not previous_dash:
            result.append("-")
            previous_dash = True
    slug = re.sub(r"-+", "-", "".join(result)).strip("-")
    if len(slug) > max_length:
        slug = slug[:max_length].rstrip("-")
    return slug or "anime"


def canonical_slug_for_item(item):
    base = slugify_text(item.get("title") or item.get("subtitle") or "anime")
    return f"{base}-{base36(item.get('id'))}"


def best_score(item):
    effective = numeric(item.get("effective_score"))
    if effective is not None:
        return effective
    aggregate = numeric(item.get("aggregate_score"))
    if aggregate is not None:
        return aggregate
    return numeric(item.get("listing_score"))


def raw_site_score(item):
    aggregate = numeric(item.get("aggregate_score"))
    if aggregate is not None:
        return aggregate
    return numeric(item.get("listing_score"))


def synthetic_rating(item):
    score = raw_site_score(item)
    count = numeric(item.get("aggregate_count")) or 0
    if score is not None:
        if count > 0:
            return ((score * count) + (SYNTHETIC_RATING_PRIOR * SYNTHETIC_RATING_MIN_COUNT)) / (count + SYNTHETIC_RATING_MIN_COUNT)
        return ((score * 5) + (SYNTHETIC_RATING_PRIOR * SYNTHETIC_RATING_MIN_COUNT)) / (5 + SYNTHETIC_RATING_MIN_COUNT)

    sources = numeric(item.get("source_count")) or 0
    available = numeric(item.get("available_episode_count")) or 0
    source_boost = min(0.45, math.log10(sources + 1) * 0.18) if sources > 0 else 0
    episode_boost = min(0.25, available / 96.0) if available > 0 else 0
    return SYNTHETIC_RATING_PRIOR - 0.35 + source_boost + episode_boost


def apply_effective_rating(item):
    external = numeric(item.get("external_score"))
    if external is not None:
        item["effective_score"] = round(external, 3)
        item["effective_score_source"] = item.get("external_score_source") or "External"
    else:
        item["synthetic_score"] = round(synthetic_rating(item), 3)
        item["effective_score"] = item["synthetic_score"]
        item["effective_score_source"] = "synthetic"
    return item


def preferred_rating_item(items):
    return sorted(
        items,
        key=lambda item: (
            0 if numeric(item.get("external_score")) is not None else 1,
            0 if (numeric(item.get("source_count")) or 0) > 0 else 1,
            -(numeric(item.get("effective_score")) or 0),
            source_priority(item.get("source")),
            item.get("id") or 0,
        ),
    )[0]


def year_number(item):
    value = numeric(item.get("year"))
    if value is not None:
        return int(value)
    published = str(item.get("date_published") or "")
    if len(published) >= 4 and published[:4].isdigit():
        return int(published[:4])
    return None


def source_priority(source):
    return SOURCE_PRIORITY.get(source or "", 99)


def source_namespace(item):
    source = item.get("source") or ""
    if source == "yummyanime":
        source_id = str(item.get("source_id") or "")
        if source_id.startswith("yummyani:") or (numeric(item.get("id")) or 0) >= 20_000_000:
            return "yummyani"
    return source


def canonical_alias_names(item):
    primary_names = {
        normalize_match_title(item.get("title")),
        normalize_match_title(item.get("subtitle")),
    }
    return {
        name
        for value in item.get("_canonical_aliases") or []
        if (name := normalize_match_title(value))
        and len(name) >= 8
        and name not in primary_names
    }


def canonical_alias_matches_other_primary(bucket, min_length=5):
    primary_names = [
        {
            normalize_match_title(item.get("title")),
            normalize_match_title(item.get("subtitle")),
        }
        for item in bucket
    ]
    for index, item in enumerate(bucket):
        own_primary = primary_names[index]
        aliases = {
            name
            for value in item.get("_canonical_aliases") or []
            if (name := normalize_match_title(value))
            and len(name) >= min_length
            and name not in own_primary
        }
        if any(aliases & other for other_index, other in enumerate(primary_names) if other_index != index):
            return True
    return False


def canonical_names(item):
    names = canonical_alias_names(item)
    for value in (item.get("title"), item.get("subtitle")):
        name = normalize_match_title(value)
        if name and len(name) >= 8:
            names.add(name)
    return names


def canonical_title_match_key(item):
    if item.get("source") not in MERGEABLE_SOURCES:
        return None
    title_key = normalize_match_title(item.get("title"))
    year = year_number(item)
    if not title_key or year is None:
        return None
    return (year, title_key)


def canonical_subtitle_match_key(item):
    if item.get("source") not in MERGEABLE_SOURCES:
        return None
    subtitle_key = normalize_match_title(item.get("subtitle"))
    year = year_number(item)
    if not subtitle_key or len(subtitle_key) < 8 or year is None:
        return None
    return (year, subtitle_key)


def canonical_title_subtitle_match_key(item):
    if item.get("source") not in MERGEABLE_SOURCES:
        return None
    title_key = normalize_match_title(item.get("title"))
    subtitle_key = normalize_match_title(item.get("subtitle"))
    if not title_key or len(title_key) < 8 or not subtitle_key or len(subtitle_key) < 8:
        return None
    return (title_key, subtitle_key)


def canonical_missing_year_title_match_key(item):
    if item.get("source") not in MERGEABLE_SOURCES:
        return None
    title_key = normalize_match_title(item.get("title"))
    if not title_key or len(title_key) < 8:
        return None
    return title_key


def variant_from_item(item):
    return {
        "id": item["id"],
        "source": item.get("source"),
        "source_id": item.get("source_id"),
        "title": item.get("title"),
        "subtitle": item.get("subtitle"),
        "url": item.get("url"),
        "year": item.get("year"),
        "scraped_at": item.get("scraped_at"),
        "source_count": item.get("source_count") or 0,
        "available_episode_count": item.get("available_episode_count") or 0,
    }


def source_sort_key(item):
    return (
        source_priority(item.get("source")),
        0 if (numeric(item.get("source_count")) or 0) > 0 else 1,
        -(best_score(item) or 0),
        str(item.get("title") or ""),
        item.get("id") or 0,
    )


def unique_values(items, getter):
    values = []
    seen = set()
    for item in items:
        raw_values = getter(item)
        if not isinstance(raw_values, list):
            raw_values = [raw_values]
        for value in raw_values:
            key = normalize_key(value)
            if not key or key in seen:
                continue
            seen.add(key)
            values.append(value)
    return values


def load_external_ratings(con):
    rows = con.execute(
        f"""
        select anime_id, label, value
        from anime_fields
        where {EXTERNAL_RATING_FIELD_PREDICATE_SQL}
        """
    ).fetchall()
    ratings = {}
    for row in rows:
        source = external_rating_source(row["label"])
        if not source:
            continue
        label, priority = source
        score = parse_rating_number(row["value"])
        if score is None:
            continue
        current = ratings.get(row["anime_id"])
        if current and current["priority"] <= priority:
            continue
        ratings[row["anime_id"]] = {
            "external_score": score,
            "external_score_source": label,
            "priority": priority,
        }
    return ratings


def apply_external_ratings(items, ratings):
    for item in items:
        rating = ratings.get(item.get("id"))
        if rating:
            item["external_score"] = rating["external_score"]
            item["external_score_source"] = rating["external_score_source"]
        else:
            item["external_score"] = None
            item["external_score_source"] = None
        apply_effective_rating(item)
    return items


def aggregate_item_state(item, variants):
    state = aggregate_state_rows(variants)
    item.update({key: value for key, value in state.items() if key != "updated_at"})
    item["state_updated_at"] = state["updated_at"]
    return item


def merge_canonical_items(items):
    sorted_items = sorted(items, key=source_sort_key)
    primary = sorted_items[0]
    merged = dict(primary)
    variants = [variant_from_item(item) for item in sorted_items]
    sources = unique_values(sorted_items, lambda item: item.get("source"))

    merged["id"] = primary["id"]
    merged["source"] = primary.get("source")
    merged["source_id"] = primary.get("source_id")
    merged["source_variants"] = variants
    merged["source_variant_count"] = len(variants)
    merged["sources"] = sources
    merged["source_member_ids"] = [variant["id"] for variant in variants]
    merged["source_count"] = sum((item.get("source_count") or 0) for item in sorted_items)
    merged["available_episode_count"] = max((item.get("available_episode_count") or 0) for item in sorted_items)
    merged["episode_count"] = max((item.get("episode_count") or 0) for item in sorted_items)
    merged["genres"] = unique_values(sorted_items, lambda item: item.get("genres") or [])
    merged["search_fields"] = unique_structured_search_fields(
        field
        for item in sorted_items
        for field in (item.get("search_fields") or [])
    )
    rating_item = preferred_rating_item(sorted_items)
    for field in ("external_score", "external_score_source", "synthetic_score", "effective_score", "effective_score_source"):
        merged[field] = rating_item.get(field)

    for field in ("subtitle", "cover_url", "listing_score", "aggregate_score", "aggregate_count", "kind", "year", "status", "episodes_text"):
        if merged.get(field) in (None, ""):
            merged[field] = next((item.get(field) for item in sorted_items if item.get(field) not in (None, "")), merged.get(field))

    merged.pop("_canonical_aliases", None)
    return aggregate_item_state(merged, sorted_items)


def can_auto_merge_by_title(bucket):
    title_key = normalize_match_title(bucket[0].get("title"))
    subtitle_keys = {normalize_match_title(item.get("subtitle")) for item in bucket if normalize_match_title(item.get("subtitle"))}
    short_title_has_matching_subtitle = len(title_key) >= 8 or len(subtitle_keys) == 1
    subtitles_do_not_conflict = len(subtitle_keys) <= 1 or canonical_alias_matches_other_primary(bucket)
    return source_namespaces_are_unique(bucket) and short_title_has_matching_subtitle and subtitles_do_not_conflict


def can_auto_merge_by_subtitle(bucket):
    return source_namespaces_are_unique(bucket)


def can_auto_merge_by_title_subtitle(bucket):
    years = [year_number(item) for item in bucket]
    known_years = {year for year in years if year is not None}
    return source_namespaces_are_unique(bucket) and any(year is None for year in years) and len(known_years) <= 1


def can_auto_merge_by_missing_year_title(bucket):
    years = [year_number(item) for item in bucket]
    known_years = {year for year in years if year is not None}
    return source_namespaces_are_unique(bucket) and any(year is None for year in years) and len(known_years) <= 1


def source_namespaces_are_unique(bucket):
    source_counts = {}
    for item in bucket:
        namespace = source_namespace(item)
        source_counts[namespace] = source_counts.get(namespace, 0) + 1
    return len(source_counts) > 1 and all(count == 1 for count in source_counts.values())


def canonical_component_metadata(items):
    return {
        "size": len(items),
        "namespaces": {source_namespace(item) for item in items},
        "years": {year_number(item) for item in items if year_number(item) is not None},
        "titles": {normalize_match_title(item.get("title")) for item in items},
        "subtitles": {normalize_match_title(item.get("subtitle")) for item in items},
        "shared_names": set.intersection(*(canonical_names(item) for item in items)),
    }


def canonical_component_metadata_is_coherent(metadata):
    if metadata["size"] < 2:
        return False
    if len(metadata["namespaces"]) != metadata["size"] or len(metadata["namespaces"]) < 2:
        return False
    if len(metadata["years"]) > 1:
        return False
    titles = metadata["titles"]
    subtitles = metadata["subtitles"]
    shares_title = len(titles) == 1 and "" not in titles
    shares_subtitle = len(subtitles) == 1 and "" not in subtitles
    return shares_title or shares_subtitle or bool(metadata.get("shared_names"))


def canonical_component_is_coherent(bucket):
    return canonical_component_metadata_is_coherent(canonical_component_metadata(bucket))


def merge_by_match_key(items, key_getter, can_merge):
    buckets = {}
    passthrough = []
    for item in items:
        key = key_getter(item)
        if key is None:
            passthrough.append(item)
        else:
            buckets.setdefault(key, []).append(item)

    merged = []
    remaining = list(passthrough)
    for bucket in buckets.values():
        if can_merge(bucket):
            merged.append(merge_canonical_items(bucket))
        else:
            remaining.extend(bucket)
    return merged, remaining


def canonical_component_root(parent, index):
    while parent[index] != index:
        parent[index] = parent[parent[index]]
        index = parent[index]
    return index


def union_canonical_components(parent, components, left, right):
    left_root = canonical_component_root(parent, left)
    right_root = canonical_component_root(parent, right)
    if left_root == right_root:
        return
    left_component = components[left_root]
    right_component = components[right_root]
    candidate = {
        "size": left_component["size"] + right_component["size"],
        "namespaces": left_component["namespaces"] | right_component["namespaces"],
        "years": left_component["years"] | right_component["years"],
        "titles": left_component["titles"] | right_component["titles"],
        "subtitles": left_component["subtitles"] | right_component["subtitles"],
        "shared_names": left_component["shared_names"] & right_component["shared_names"],
    }
    if canonical_component_metadata_is_coherent(candidate):
        parent[right_root] = left_root
        components[left_root] = candidate
        components[right_root] = None


def union_merge_indices(items, key_getter, can_merge, parent, components):
    buckets = {}
    for index, item in enumerate(items):
        key = key_getter(item)
        if key is not None:
            buckets.setdefault(key, []).append(index)

    for indices in buckets.values():
        bucket = [items[index] for index in indices]
        if can_merge(bucket):
            first = indices[0]
            for index in indices[1:]:
                union_canonical_components(parent, components, first, index)


def union_merge_alias_indices(items, parent, components):
    buckets = {}
    alias_evidence = {}
    for index, item in enumerate(items):
        year = year_number(item)
        if item.get("source") not in MERGEABLE_SOURCES or year is None:
            continue
        aliases = canonical_alias_names(item)
        for name in canonical_names(item):
            key = (year, name)
            buckets.setdefault(key, []).append(index)
            if name in aliases:
                alias_evidence.setdefault(key, set()).add(index)

    for key, indices in buckets.items():
        if len(indices) < 2 or not alias_evidence.get(key):
            continue
        bucket = [items[index] for index in indices]
        if not source_namespaces_are_unique(bucket):
            continue
        first = indices[0]
        for index in indices[1:]:
            union_canonical_components(parent, components, first, index)


def canonicalize_items(items):
    parent = list(range(len(items)))
    components = [canonical_component_metadata([item]) for item in items]
    merge_rules = (
        (canonical_subtitle_match_key, can_auto_merge_by_subtitle),
        (canonical_title_match_key, can_auto_merge_by_title),
        (canonical_title_subtitle_match_key, can_auto_merge_by_title_subtitle),
        (canonical_missing_year_title_match_key, can_auto_merge_by_missing_year_title),
    )
    for key_getter, can_merge in merge_rules:
        union_merge_indices(items, key_getter, can_merge, parent, components)
    union_merge_alias_indices(items, parent, components)

    def find(index):
        return canonical_component_root(parent, index)

    buckets = {}
    for index, item in enumerate(items):
        buckets.setdefault(find(index), []).append(item)
    groups = [merge_canonical_items(bucket) for bucket in buckets.values()]

    for group in groups:
        slug = canonical_slug_for_item(group)
        group["slug"] = slug
        group["internal_id"] = slug
    groups.sort(key=lambda item: ((numeric(item.get("source_count")) or 0) <= 0, -(item.get("id") or 0)))
    return groups


def item_matches_query(item, query):
    if not query:
        return True
    return item_search_score(item, search_query_info(query)) > 0


def parse_json_object(value):
    try:
        payload = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def russian_plural(count, one, few, many):
    count = abs(int(count))
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


def update_event_payload(row):
    event = dict(row)
    event["metadata"] = parse_json_object(event.pop("metadata_json", "{}"))
    return event


def recent_update_summary(events, days=content_updates.RECENT_UPDATE_DAYS):
    if not events:
        return None
    counts = {}
    for event in events:
        event_type = event.get("display_event_type") or event["event_type"]
        counts[event_type] = counts.get(event_type, 0) + 1

    if counts.get("new_title"):
        badge = "новый"
        label = "Новый тайтл"
    elif counts.get("new_episode"):
        count = counts["new_episode"]
        word = russian_plural(count, "серия", "серии", "серий")
        badge = f"+{count} {word}"
        label = f"Добавлено {count} {word}"
    elif counts.get("new_translation"):
        count = counts["new_translation"]
        word = russian_plural(count, "озвучка", "озвучки", "озвучек")
        badge = f"+{count} {word}"
        label = f"Добавлено {count} {word}"
    else:
        count = counts.get("new_provider", len(events))
        word = russian_plural(count, "плеер", "плеера", "плееров")
        badge = f"+{count} {word}"
        label = f"Добавлено {count} {word}"

    return {
        "badge": badge,
        "label": label,
        "count": len(events),
        "event_counts": counts,
        "latest_at": events[0]["occurred_at"],
        "days": days,
    }


def load_recent_update_events(con, anime_ids, days=content_updates.RECENT_UPDATE_DAYS):
    anime_ids = [int(value) for value in anime_ids if value is not None]
    if not anime_ids:
        return []
    placeholders = ",".join("?" for _ in anime_ids)
    rows = con.execute(
        f"""
        select
            id,
            run_id,
            event_type,
            anime_id,
            episode_id,
            video_source_id,
            source,
            source_id,
            episode_number,
            translation_title,
            provider_title,
            title,
            description,
            occurred_at,
            metadata_json
        from content_update_events
        where anime_id in ({placeholders})
          and occurred_at >= ?
        order by occurred_at desc, id desc
        """,
        (*anime_ids, content_updates.recent_cutoff(days)),
    ).fetchall()
    return [update_event_payload(row) for row in rows]


def attach_recent_updates(con, items):
    source_ids = []
    for item in items:
        source_ids.extend(item.get("source_member_ids") or [item.get("id")])
    events = load_recent_update_events(con, source_ids)
    events_by_anime_id = {}
    for event in events:
        events_by_anime_id.setdefault(event["anime_id"], []).append(event)

    for item in items:
        member_ids = item.get("source_member_ids") or [item.get("id")]
        item_events = []
        for anime_id in member_ids:
            item_events.extend(events_by_anime_id.get(anime_id, []))
        item_events.sort(key=lambda event: (event["occurred_at"], event["id"]), reverse=True)
        item["recent_updates"] = item_events[:12]
        item["recent_update_summary"] = recent_update_summary(item["recent_updates"])
    return items


def normalize_content_update_limit(value):
    try:
        requested = int(value)
    except (TypeError, ValueError):
        requested = DEFAULT_CONTENT_UPDATE_LIMIT
    return max(1, min(MAX_CONTENT_UPDATE_LIMIT, requested))


def normalize_content_update_days(value):
    if value is None:
        return DEFAULT_CONTENT_UPDATE_DAYS
    text = str(value).strip().lower()
    if text in {"", "default"}:
        return DEFAULT_CONTENT_UPDATE_DAYS
    if text in {"all", "any", "0"}:
        return None
    try:
        requested = int(text)
    except ValueError:
        return DEFAULT_CONTENT_UPDATE_DAYS
    return max(1, min(365, requested))


def normalize_content_update_offset(value):
    try:
        requested = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, requested)


def normalize_content_update_type(value):
    requested = str(value or "all").strip().lower()
    if requested == "all":
        return requested
    if requested not in content_updates.EVENT_TYPES:
        raise ValueError("invalid content update event_type")
    return requested


def content_update_period_payload(days):
    if days is None:
        return {"days": None, "label": "последние"}
    return {"days": days, "label": f"за {days} дн."}


def content_update_where(days, event_type):
    clauses = []
    params = []
    if days is not None:
        clauses.append("occurred_at >= ?")
        params.append(content_updates.recent_cutoff(days))
    if event_type != "all":
        clauses.append("event_type = ?")
        params.append(event_type)
    where = f"where {' and '.join(clauses)}" if clauses else ""
    return where, params


def load_content_update_rows(con, days, limit, event_type="all", offset=0):
    where, params = content_update_where(days, event_type)
    rows = con.execute(
        f"""
        select
            id,
            run_id,
            event_type,
            anime_id,
            episode_id,
            video_source_id,
            source,
            source_id,
            episode_number,
            translation_title,
            provider_title,
            title,
            description,
            occurred_at,
            metadata_json
        from content_update_events
        {where}
        order by occurred_at desc, id desc
        limit ? offset ?
        """,
        (*params, limit + 1, offset),
    ).fetchall()
    return [update_event_payload(row) for row in rows]


def load_content_update_source_summaries(con, days, event_type="all"):
    where, params = content_update_where(days, event_type)
    return con.execute(
        f"""
        select
            anime_id,
            max(occurred_at) as latest_at,
            max(id) as latest_event_id,
            count(*) as event_count
        from content_update_events
        {where}
        group by anime_id
        """,
        params,
    ).fetchall()


def load_content_update_rows_for_anime_ids(con, days, event_type, anime_ids):
    anime_ids = sorted({int(value) for value in anime_ids if value is not None})
    if not anime_ids:
        return []
    where, params = content_update_where(days, event_type)
    prefix = f"{where} and" if where else "where"
    rows = con.execute(
        f"""
        select
            id,
            run_id,
            event_type,
            anime_id,
            episode_id,
            video_source_id,
            source,
            source_id,
            episode_number,
            translation_title,
            provider_title,
            title,
            description,
            occurred_at,
            metadata_json
        from content_update_events
        {prefix} anime_id in ({sql_placeholders(anime_ids)})
        order by occurred_at desc, id desc
        """,
        (*params, *anime_ids),
    ).fetchall()
    return [update_event_payload(row) for row in rows]


def content_update_total_summary(con, cache, days, event_type):
    where, params = content_update_where(days, event_type)
    count_rows = con.execute(
        f"""
        select event_type, count(*) as event_count
        from content_update_events
        {where}
        group by event_type
        """,
        params,
    ).fetchall()
    counts = {event_type: 0 for event_type in sorted(content_updates.EVENT_TYPES)}
    for row in count_rows:
        counts[row["event_type"]] = int(row["event_count"] or 0)

    anime_rows = con.execute(
        f"select distinct anime_id from content_update_events {where}",
        params,
    ).fetchall()
    canonical_ids = set()
    id_map = cache.get("id_map", {})
    for row in anime_rows:
        source_anime_id = int(row["anime_id"])
        group = id_map.get(source_anime_id)
        canonical_ids.add(("canonical", group["id"]) if group else ("source", source_anime_id))
    return {
        "event_count": sum(counts.values()),
        "updated_title_count": len(canonical_ids),
        "event_counts": counts,
    }


def latest_content_update_run(con):
    row = con.execute(
        """
        select id, mode, trigger, sources_json, started_at, finished_at, duration_ms, status, stats_json, error
        from content_update_runs
        order by started_at desc, id desc
        limit 1
        """
    ).fetchone()
    if not row:
        return None
    payload = dict(row)
    payload["sources"] = json.loads(payload.pop("sources_json") or "[]")
    payload["stats"] = parse_json_object(payload.pop("stats_json", "{}"))
    return payload


def content_update_value_sort_key(value):
    number = numeric(value)
    return (0, number) if number is not None else (1, str(value or ""))


def content_update_report(events):
    counts = {event_type: 0 for event_type in sorted(content_updates.EVENT_TYPES)}
    episode_numbers = set()
    translations = {}
    providers = {}
    new_episode_provider_count = 0
    new_title_episode_count = 0
    new_title_provider_count = 0
    new_title_translations = set()

    for event in events:
        event_type = event.get("display_event_type") or event.get("event_type")
        counts[event_type] = counts.get(event_type, 0) + 1
        episode_number = str(event.get("episode_number") or "").strip()
        if event_type == "new_episode":
            if episode_number:
                episode_numbers.add(episode_number)
            new_episode_provider_count += int(numeric((event.get("metadata") or {}).get("provider_count")) or 0)
        elif event_type == "new_translation":
            title = str(event.get("translation_title") or "Без названия").strip()
            key = normalize_key(title)
            entry = translations.setdefault(key, {"title": title, "episode_numbers": set(), "event_count": 0})
            entry["event_count"] += 1
            if episode_number:
                entry["episode_numbers"].add(episode_number)
        elif event_type == "new_provider":
            title = str(event.get("provider_title") or "Без названия").strip()
            translation_title = str(event.get("translation_title") or "").strip()
            key = (normalize_key(title), normalize_key(translation_title))
            entry = providers.setdefault(
                key,
                {
                    "title": title,
                    "translation_title": translation_title or None,
                    "episode_numbers": set(),
                    "event_count": 0,
                },
            )
            entry["event_count"] += 1
            if episode_number:
                entry["episode_numbers"].add(episode_number)
        elif event_type == "new_title":
            metadata = event.get("metadata") or {}
            new_title_episode_count = max(new_title_episode_count, int(numeric(metadata.get("episode_count")) or 0))
            new_title_provider_count = max(new_title_provider_count, int(numeric(metadata.get("provider_count")) or 0))
            new_title_translations.update(
                str(value).strip()
                for value in metadata.get("translations") or []
                if str(value).strip()
            )

    def entries_payload(entries):
        result = []
        for entry in sorted(
            entries.values(),
            key=lambda value: (
                normalize_key(value["title"]),
                normalize_key(value.get("translation_title")),
            ),
        ):
            numbers = sorted(entry["episode_numbers"], key=content_update_value_sort_key)
            payload = {
                "title": entry["title"],
                "episode_numbers": numbers,
                "episode_count": len(numbers),
                "event_count": entry["event_count"],
            }
            if entry.get("translation_title"):
                payload["translation_title"] = entry["translation_title"]
            result.append(payload)
        return result

    return {
        "event_count": len(events),
        "event_counts": counts,
        "new_title": {
            "count": counts.get("new_title", 0),
            "episode_count": new_title_episode_count,
            "provider_count": new_title_provider_count,
            "translations": sorted(new_title_translations, key=normalize_key),
        },
        "episode_numbers": sorted(episode_numbers, key=content_update_value_sort_key),
        "new_episode_provider_count": new_episode_provider_count,
        "translations": entries_payload(translations),
        "providers": entries_payload(providers),
    }


def content_update_item_is_priority(item):
    return bool(item.get("is_favorite")) or item.get("watch_status") == "watching"


def compact_content_update_item(item, events, days):
    payload = {
        key: item.get(key)
        for key in (
            "id",
            "slug",
            "title",
            "subtitle",
            "cover_url",
            "kind",
            "status",
            "year",
            "source",
            "source_count",
            "available_episode_count",
            "is_favorite",
            "watched",
            "progress_episode_number",
            "watch_status",
        )
        if keep_public_value(item.get(key))
    }
    payload.update(
        {
            "is_favorite": bool(item.get("is_favorite")),
            "watched": bool(item.get("watched")),
            "progress_episode_number": item.get("progress_episode_number"),
            "watch_status": item.get("watch_status"),
            "is_priority": content_update_item_is_priority(item),
        }
    )
    sources = list(item.get("sources") or [])
    if sources:
        payload["sources"] = sources
    payload["latest_update_at"] = events[0]["occurred_at"] if events else None
    payload["recent_update_summary"] = recent_update_summary(events, days)
    payload["report"] = content_update_report(events)
    # Reports contain the complete aggregation. Keep just the newest event for
    # navigation/backward compatibility instead of duplicating large feeds.
    payload["events"] = [dict(event) for event in events[:1]]
    return payload


def content_update_event_with_catalog(event, group):
    payload = dict(event)
    payload["source_anime_id"] = event.get("anime_id")
    if not group:
        return payload
    payload["anime_id"] = group["id"]
    payload["anime_ref"] = group.get("slug") or group.get("internal_id") or group["id"]
    payload["anime_slug"] = group.get("slug")
    payload["anime_title"] = group.get("title")
    payload["anime_subtitle"] = group.get("subtitle")
    payload["cover_url"] = group.get("cover_url")
    if not payload.get("title"):
        payload["title"] = group.get("title")
    return payload


def get_content_updates(
    db_path=None,
    days=DEFAULT_CONTENT_UPDATE_DAYS,
    limit=DEFAULT_CONTENT_UPDATE_LIMIT,
    user_id=None,
    event_type="all",
    offset=None,
):
    days = normalize_content_update_days(days)
    limit = normalize_content_update_limit(limit)
    event_type = normalize_content_update_type(event_type)
    include_offset_pagination = offset is not None
    offset = normalize_content_update_offset(offset)
    path = resolve_db_path(db_path)
    con = connect(path)
    try:
        cache = get_catalog_cache(path, connection=con)
        user_state_by_source_id = load_user_state_by_source_id(
            path,
            user_id,
            connection=con,
        )
        source_summaries = load_content_update_source_summaries(con, days, event_type)
        summary = content_update_total_summary(con, cache, days, event_type)
        latest_run = latest_content_update_run(con)
        grouped = {}
        for row in source_summaries:
            group = cache.get("id_map", {}).get(int(row["anime_id"]))
            if not group:
                continue
            current = grouped.setdefault(
                group["id"],
                {
                    "group": group,
                    "latest_at": row["latest_at"],
                    "latest_event_id": int(row["latest_event_id"] or 0),
                    "event_count": 0,
                },
            )
            current["event_count"] += int(row["event_count"] or 0)
            if (row["latest_at"], int(row["latest_event_id"] or 0)) > (
                current["latest_at"],
                current["latest_event_id"],
            ):
                current["latest_at"] = row["latest_at"]
                current["latest_event_id"] = int(row["latest_event_id"] or 0)

        for entry in grouped.values():
            entry["item"] = clone_catalog_item(
                entry["group"],
                user_state_by_source_id=user_state_by_source_id,
            )
            entry["is_priority"] = content_update_item_is_priority(entry["item"])
        ordered_groups = sorted(
            grouped.values(),
            key=lambda entry: (
                entry["is_priority"],
                entry["latest_at"] or "",
                entry["latest_event_id"],
                entry["group"]["id"],
            ),
            reverse=True,
        )
        page_groups = ordered_groups[offset : offset + limit]
        source_anime_ids = {
            source_id
            for entry in page_groups
            for source_id in entry["group"].get("source_member_ids") or [entry["group"]["id"]]
        }
        raw_events = load_content_update_rows_for_anime_ids(
            con,
            days,
            event_type,
            source_anime_ids,
        )
    finally:
        con.close()

    events = []
    events_by_item_id = {}
    for raw_event in raw_events:
        group = cache.get("id_map", {}).get(int(raw_event["anime_id"]))
        event = content_update_event_with_catalog(raw_event, group)
        events.append(event)
        if group:
            events_by_item_id.setdefault(group["id"], []).append(event)

    items = []
    for entry in page_groups:
        item_events = events_by_item_id.get(entry["group"]["id"], [])
        item_events.sort(key=lambda event: (event["occurred_at"], event["id"]), reverse=True)
        items.append(compact_content_update_item(entry["item"], item_events, days))

    has_more = offset + len(items) < len(ordered_groups)
    preview_events = [event for item in items for event in item["events"]]
    preview_events.sort(key=lambda event: (event["occurred_at"], event["id"]), reverse=True)

    pagination = {
        "limit": limit,
        "returned": len(items),
        "returned_events": sum(item["report"]["event_count"] for item in items),
        "has_more": has_more,
    }
    if include_offset_pagination:
        pagination.update(
            {
                "offset": offset,
                "next_offset": offset + len(items) if has_more else None,
            }
        )

    return {
        "period": content_update_period_payload(days),
        "event_type": event_type,
        "summary": summary,
        "items": items,
        "events": preview_events,
        "latest_run": latest_run,
        "pagination": pagination,
    }


def format_number(value):
    number = numeric(value)
    if number is None:
        return ""
    return f"{number:.1f}".rstrip("0").rstrip(".")


def recommendation_confidence(score):
    if score >= 76:
        return "высокая"
    if score >= 60:
        return "средняя"
    return "осторожная"


def normalize_recommendation_limit(limit):
    try:
        requested = int(limit)
    except (TypeError, ValueError):
        requested = DEFAULT_RECOMMENDATION_LIMIT
    return max(1, min(MAX_RECOMMENDATION_LIMIT, requested))


def seed_weight(item):
    return recommendation_model.seed_weight(item)


def item_genre_keys(item):
    if "_genre_keys" not in item:
        keys = set()
        for genre in item.get("genres", []):
            key = normalize_key(genre)
            if key:
                keys.add(key)
        item["_genre_keys"] = keys
    return item["_genre_keys"]


def item_genre_label_map(item):
    if "_genre_label_map" not in item:
        labels = {}
        for genre in item.get("genres", []):
            key = normalize_key(genre)
            if key:
                labels.setdefault(key, genre)
        item["_genre_label_map"] = labels
    return item["_genre_label_map"]


def public_item_copy(item):
    return {key: value for key, value in item.items() if not key.startswith("_")}


def build_recommendation_profile(items):
    seeds = [item for item in items if seed_weight(item) > 0]
    genre_weights = {}
    genre_labels = {}
    kind_weights = {}

    for item in seeds:
        weight = seed_weight(item)
        label_map = item_genre_label_map(item)
        for key in item_genre_keys(item):
            genre_weights[key] = genre_weights.get(key, 0.0) + weight
            genre_labels.setdefault(key, label_map[key])
        kind = item.get("kind")
        if kind:
            kind_weights[kind] = kind_weights.get(kind, 0.0) + weight

    top_genres = sorted(
        (
            {"genre": genre_labels[key], "weight": round(weight, 2)}
            for key, weight in genre_weights.items()
        ),
        key=lambda item: (-item["weight"], item["genre"]),
    )[:8]

    return {
        "seeds": seeds,
        "favorite_count": sum(1 for item in items if item.get("is_favorite")),
        "seed_count": len(seeds),
        "genre_weights": genre_weights,
        "genre_weight_desc": sorted(genre_weights.values(), reverse=True),
        "kind_weights": kind_weights,
        "top_genres": top_genres,
    }


def genre_profile_score(candidate, profile):
    candidate_keys = item_genre_keys(candidate)
    if not candidate_keys or not profile["genre_weights"]:
        return 0.0, []

    matched_weight = sum(profile["genre_weights"].get(key, 0.0) for key in candidate_keys)
    comparison_size = max(3, len(candidate_keys))
    top_possible = sum(profile["genre_weight_desc"][:comparison_size])
    score = clamp(matched_weight / top_possible) if top_possible else 0.0
    label_map = item_genre_label_map(candidate)
    matched = [
        label
        for key, label in label_map.items()
        if key in profile["genre_weights"]
    ]
    return score, matched


def seed_similarity(candidate, profile):
    candidate_keys = item_genre_keys(candidate)
    if not candidate_keys:
        return 0.0, []

    matches = []
    for seed in profile["seeds"]:
        seed_keys = item_genre_keys(seed)
        if not seed_keys:
            continue
        overlap = candidate_keys & seed_keys
        if not overlap:
            continue
        union = candidate_keys | seed_keys
        score = len(overlap) / len(union)
        if candidate.get("kind") and candidate.get("kind") == seed.get("kind"):
            score += 0.05
        label_map = item_genre_label_map(candidate)
        matches.append(
            {
                "id": seed["id"],
                "title": seed["title"],
                "score": round(clamp(score), 3),
                "matched_genres": [
                    label
                    for key, label in label_map.items()
                    if key in overlap
                ],
            }
        )

    matches.sort(key=lambda item: item["score"], reverse=True)
    best = matches[0]["score"] if matches else 0.0
    return best, matches[:2]


def quality_score(item):
    score = best_score(item)
    if score is None:
        return 0.35
    return clamp((score - 5.8) / 3.7)


def popularity_score(item):
    count = numeric(item.get("aggregate_count")) or 0
    return clamp(math.log10(count + 1) / 4.0) if count > 0 else 0.0


def has_playable_source(item):
    return (numeric(item.get("source_count")) or 0) > 0


def availability_score(item):
    available = numeric(item.get("available_episode_count")) or 0
    sources = numeric(item.get("source_count")) or 0
    if sources > 0:
        return clamp(0.75 + min(0.25, available / 48.0))
    if (numeric(item.get("episode_count")) or 0) > 0:
        return 0.25
    return 0.0


def recency_score(item):
    year = year_number(item)
    if year is None:
        return 0.35
    current_year = dt.datetime.now().year
    if year >= current_year:
        return 1.0
    if year == current_year - 1:
        return 0.85
    if year == current_year - 2:
        return 0.7
    if year >= current_year - 5:
        return 0.55
    return 0.4


def kind_profile_score(item, profile):
    kind = item.get("kind")
    if not kind or not profile["kind_weights"]:
        return 0.0
    max_weight = max(profile["kind_weights"].values())
    return clamp(profile["kind_weights"].get(kind, 0.0) / max_weight) if max_weight else 0.0


def watchable_recommendation_score(raw_score, item, has_watchable_candidates):
    score = clamp(raw_score) * 100
    if not has_watchable_candidates:
        return round(score, 1)
    if has_playable_source(item):
        return round(55 + (score * 0.45), 1)
    return round(score * 0.55, 1)


def recommendation_reasons(item, matched_genres, based_on):
    reasons = []
    if matched_genres:
        reasons.append(f"Совпали жанры: {', '.join(matched_genres[:4])}")
    if based_on:
        titles = ", ".join(match["title"] for match in based_on[:2])
        reasons.append(f"Похоже на: {titles}")

    score = best_score(item)
    if score is not None:
        rating = format_number(score)
        source = item.get("effective_score_source")
        if source == "synthetic":
            suffix = " синт."
        elif source:
            suffix = f" {source}"
        else:
            count = int(numeric(item.get("aggregate_count")) or 0)
            suffix = f" ({count} оценок)" if count >= 10 else ""
        reasons.append(f"Рейтинг {rating}/10{suffix}")

    available = int(numeric(item.get("available_episode_count")) or 0)
    if available:
        reasons.append(f"Есть видео: {available} сер.")
    elif item.get("source_count"):
        reasons.append("Есть доступные источники")
    else:
        reasons.append("Пока без видео в базе")

    if not reasons:
        reasons.append("Хороший общий рейтинг для стартовой рекомендации")
    return reasons[:4]


def normalize_recommendation_filters(filters=None):
    filters = filters or {}
    normalized = {}
    for key in ("genre", "year", "year_from", "year_to", "kind", "status", "source"):
        value = filters.get(key)
        if value not in (None, ""):
            normalized[key] = value
    video = str(filters.get("video") or "").strip().lower()
    if video == "with":
        normalized["video"] = True
    elif video == "missing":
        normalized["video"] = False
    return normalized


def get_recommendations(
    db_path=None,
    limit=DEFAULT_RECOMMENDATION_LIMIT,
    user_id=None,
    filters=None,
):
    """Return deterministic Recommendation v2 results for one user."""

    limit = normalize_recommendation_limit(limit or DEFAULT_RECOMMENDATION_LIMIT)
    items = get_anime_list(db_path, user_id=user_id)
    normalized_filters = normalize_recommendation_filters(filters)
    payload = recommendation_model.rank_recommendations(
        items,
        limit=limit,
        filters=normalized_filters,
    )
    unknown_items = [item for item in items if not recommendation_model.is_known_item(item)]
    filtered_unknown_items = recommendation_model.filter_catalog_items(
        unknown_items,
        filters=normalized_filters,
    )
    payload["profile"]["watchable_candidate_count"] = sum(
        1 for item in filtered_unknown_items if recommendation_model.has_playable_source(item)
    )
    return payload


def load_title_alias_search_fields(con):
    rows = con.execute(
        """
        select anime_id, alias, alias_type, source
        from anime_title_aliases
        where alias is not null and trim(alias) <> ''
        order by anime_id, source, alias_type, alias
        """
    ).fetchall()
    fields_by_anime_id = {}
    for row in rows:
        alias_type = row["alias_type"] or "alias"
        weight = TITLE_ALIAS_SEARCH_WEIGHTS.get(alias_type, TITLE_ALIAS_SEARCH_WEIGHTS["other_title"])
        field = make_search_field(
            row["alias"],
            weight,
            "alias",
            label=alias_type,
            source=row["source"] or "manual",
        )
        if field:
            fields_by_anime_id.setdefault(row["anime_id"], []).append(field)
    return {
        anime_id: unique_structured_search_fields(fields)
        for anime_id, fields in fields_by_anime_id.items()
    }


def load_metadata_search_fields(con):
    other_titles_label = "Другие названия"
    labels = list(SEARCH_METADATA_LABEL_WEIGHTS) + [other_titles_label]
    rows = con.execute(
        f"""
        select anime_id, label, value
        from anime_fields
        where label in ({sql_placeholders(labels)})
          and value is not null
          and trim(value) <> ''
        order by anime_id, label
        """,
        labels,
    ).fetchall()
    fields_by_anime_id = {}
    for row in rows:
        label = row["label"]
        values = split_search_values(row["value"])
        fields = fields_by_anime_id.setdefault(row["anime_id"], [])
        if label == other_titles_label:
            for value in values:
                field = make_search_field(
                    value,
                    TITLE_ALIAS_SEARCH_WEIGHTS["other_title"],
                    "alias",
                    label="other_title",
                    source="anime_fields",
                )
                if field:
                    fields.append(field)
            continue

        weight = SEARCH_METADATA_LABEL_WEIGHTS[label]
        for value in values:
            field = make_search_field(value, weight, "metadata", label=label, source="anime_fields")
            if field:
                fields.append(field)
            labeled_field = make_search_field(
                f"{label} {value}",
                max(weight - 1, 1),
                "metadata",
                label=label,
                source="anime_fields",
            )
            if labeled_field:
                fields.append(labeled_field)
    return {
        anime_id: unique_structured_search_fields(fields)
        for anime_id, fields in fields_by_anime_id.items()
    }


def load_canonical_match_aliases(con):
    aliases = {}
    for row in con.execute(
        """
        select anime_id, alias
        from anime_title_aliases
        where alias is not null and trim(alias) <> ''
        """
    ).fetchall():
        aliases.setdefault(row["anime_id"], []).append(row["alias"])
    for row in con.execute(
        """
        select anime_id, value
        from anime_fields
        where label = 'Другие названия'
          and value is not null
          and trim(value) <> ''
        """
    ).fetchall():
        # YummyAni serializes exact alternative titles with semicolons. Commas
        # are valid title punctuation and must not create partial merge keys.
        aliases.setdefault(row["anime_id"], []).extend(
            value.strip()
            for value in str(row["value"]).split(";")
            if value.strip()
        )
    return aliases


def load_catalog_search_fields(con):
    title_alias_search_fields = load_title_alias_search_fields(con)
    metadata_search_fields = load_metadata_search_fields(con)
    anime_ids = set(title_alias_search_fields) | set(metadata_search_fields)
    return {
        anime_id: unique_structured_search_fields(
            (title_alias_search_fields.get(anime_id) or [])
            + (metadata_search_fields.get(anime_id) or [])
        )
        for anime_id in anime_ids
    }


def get_source_anime_items(con, user_id=None, include_search_fields=True):
    user_id = resolved_user_id(con, user_id)
    rows = con.execute(
        """
        with
            episode_counts as (
                select anime_id, count(*) as episode_count
                from episodes
                group by anime_id
            ),
            available_episode_counts as (
                select e.anime_id, count(distinct e.id) as available_episode_count
                from episodes e
                join video_sources vs on vs.episode_id = e.id
                where vs.embed_url is not null
                group by e.anime_id
            ),
            source_counts as (
                select anime_id, count(*) as source_count
                from video_sources
                where embed_url is not null
                group by anime_id
            ),
            genre_lists as (
                select anime_id, group_concat(genre) as genres
                from anime_genres
                group by anime_id
            )
        select
            a.id,
            a.title,
            a.subtitle,
            a.url,
            a.cover_url,
            a.source,
            a.source_id,
            a.listing_score,
            a.aggregate_score,
            a.aggregate_count,
            a.date_published,
            a.kind,
            a.year,
            a.status,
            a.episodes_text,
            a.scraped_at,
            coalesce(us.is_favorite, 0) as is_favorite,
            us.progress_episode_number,
            coalesce(us.watched, 0) as watched,
            us.watch_status,
            coalesce(us.not_interested, 0) as not_interested,
            us.updated_at as state_updated_at,
            us.favorite_updated_at,
            us.watch_status_updated_at,
            us.not_interested_updated_at,
            coalesce(ec.episode_count, 0) as episode_count,
            coalesce(aec.available_episode_count, 0) as available_episode_count,
            coalesce(sc.source_count, 0) as source_count,
            gl.genres
        from anime a
        left join user_title_state us on us.anime_id = a.id and us.user_id = ?
        left join episode_counts ec on ec.anime_id = a.id
        left join available_episode_counts aec on aec.anime_id = a.id
        left join source_counts sc on sc.anime_id = a.id
        left join genre_lists gl on gl.anime_id = a.id
        order by source_count > 0 desc, a.id desc
        """,
        (user_id,),
    ).fetchall()

    items = rows_to_dicts(rows)
    apply_external_ratings(items, load_external_ratings(con))
    search_fields_by_anime_id = load_catalog_search_fields(con) if include_search_fields else {}
    canonical_aliases_by_anime_id = load_canonical_match_aliases(con)
    for item in items:
        apply_state_fields(item)
        item["genres"] = [g for g in (item.pop("genres") or "").split(",") if g]
        item["available_episode_count"] = item["available_episode_count"] or 0
        item["search_fields"] = [dict(field) for field in search_fields_by_anime_id.get(item["id"], [])]
        item["_canonical_aliases"] = list(canonical_aliases_by_anime_id.get(item["id"], []))
    return items


def build_translation_rankings(con):
    rows = con.execute(
        """
        select distinct
            vs.anime_id,
            vs.translation_title
        from video_sources vs
        where vs.embed_url is not null
          and vs.translation_title is not null
        """
    ).fetchall()

    anime_ids_by_key = {}
    label_by_key = {}
    for row in rows:
        key = translation_key(row["translation_title"])
        if key in GENERIC_TRANSLATION_KEYS:
            continue
        anime_ids_by_key.setdefault(key, set()).add(row["anime_id"])
        label_by_key.setdefault(key, row["translation_title"])

    ranked_keys = sorted(
        anime_ids_by_key,
        key=lambda key: (-len(anime_ids_by_key[key]), label_by_key.get(key) or key),
    )
    return {
        key: {
            "rank": index,
            "title_count": len(anime_ids_by_key[key]),
            "label": label_by_key.get(key) or key,
        }
        for index, key in enumerate(ranked_keys)
    }


def get_anime_list(db_path=None, q=None, user_id=None, include_search_fields=False):
    path = resolve_db_path(db_path)
    con = connect(path)
    try:
        cache = get_catalog_cache(path, user_id, connection=con)
        user_state_by_source_id = load_user_state_by_source_id(
            path,
            user_id,
            connection=con,
        )
    finally:
        con.close()
    items = cache["items"]
    if not q:
        return clone_catalog_items(
            items,
            include_search_fields=include_search_fields,
            user_state_by_source_id=user_state_by_source_id,
        )
    query = search_query_info(q)
    search_indexes = catalog_search_indexes(cache, db_path)
    scored = [
        (search_index_score(search_indexes.get(item["id"]) or item_search_index(item), query), index, item)
        for index, item in enumerate(items)
    ]
    return [
        clone_catalog_item(
            item,
            include_search_fields=include_search_fields,
            user_state_by_source_id=user_state_by_source_id,
        )
        for score, _, item in sorted(scored, key=lambda entry: (-entry[0], entry[1]))
        if score > 0
    ]


def sql_placeholders(values):
    return ",".join("?" for _ in values)


def load_user_state_by_source_id(db_path=None, user_id=None, connection=None):
    if user_id is None:
        return None
    owns_connection = connection is None
    con = connection
    if con is None:
        path = resolve_db_path(db_path)
        con = sqlite3.connect(path)
        con.execute("pragma busy_timeout=30000")
        con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            with
                watch_stats as (
                    select
                        anime_id,
                        sum(engaged_seconds) as watch_engaged_seconds,
                        sum(case when engaged_seconds >= ? then engaged_seconds else 0 end) as meaningful_watch_seconds,
                        sum(case when engaged_seconds >= ? then 1 else 0 end) as meaningful_watch_episode_count,
                        max(last_seen_at) as watch_last_seen_at
                    from user_episode_state
                    where user_id = ?
                      and started_at is not null
                    group by anime_id
                ),
                anime_keys as (
                    select anime_id
                    from user_title_state
                    where user_id = ?
                    union
                    select anime_id
                    from watch_stats
                )
            select
                k.anime_id,
                coalesce(us.is_favorite, 0) as is_favorite,
                us.progress_episode_number,
                coalesce(us.watched, 0) as watched,
                us.watch_status,
                coalesce(us.not_interested, 0) as not_interested,
                us.updated_at,
                us.favorite_updated_at,
                us.watch_status_updated_at,
                us.not_interested_updated_at,
                coalesce(ws.watch_engaged_seconds, 0) as watch_engaged_seconds,
                coalesce(ws.meaningful_watch_seconds, 0) as meaningful_watch_seconds,
                coalesce(ws.meaningful_watch_episode_count, 0) as meaningful_watch_episode_count,
                ws.watch_last_seen_at
            from anime_keys k
            left join user_title_state us
              on us.anime_id = k.anime_id
             and us.user_id = ?
            left join watch_stats ws
              on ws.anime_id = k.anime_id
            """,
            (
                MEANINGFUL_WATCH_SECONDS,
                MEANINGFUL_WATCH_SECONDS,
                int(user_id),
                int(user_id),
                int(user_id),
            ),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        if owns_connection:
            con.close()
    return {row["anime_id"]: dict(row) for row in rows}


def catalog_item_user_state(item, user_state_by_source_id):
    if user_state_by_source_id is None:
        return None
    rows = [
        user_state_by_source_id[source_id]
        for source_id in item.get("source_member_ids") or [item.get("id")]
        if source_id in user_state_by_source_id
    ]
    return aggregate_state_rows(rows)


def clone_catalog_item(item, include_search_fields=False, user_state_by_source_id=None):
    cloned = dict(item)
    user_state = catalog_item_user_state(item, user_state_by_source_id)
    if user_state is not None:
        cloned.update(user_state)
    cloned["genres"] = list(item.get("genres") or [])
    cloned["sources"] = list(item.get("sources") or [])
    cloned["source_member_ids"] = list(item.get("source_member_ids") or [])
    cloned["source_variants"] = [compact_catalog_variant(variant) for variant in item.get("source_variants") or []]
    if include_search_fields:
        cloned["search_fields"] = [dict(field) for field in item.get("search_fields") or []]
    else:
        cloned.pop("search_fields", None)
    cloned["recent_updates"] = [dict(update) for update in item.get("recent_updates") or []]
    cloned["recent_update_summary"] = dict(item["recent_update_summary"]) if item.get("recent_update_summary") else None
    return cloned


def compact_catalog_variant(variant):
    return {
        key: variant.get(key)
        for key in ("id", "source", "title", "subtitle")
        if variant.get(key) not in (None, "")
    }


CATALOG_API_ITEM_FIELDS = (
    "id",
    "slug",
    "title",
    "subtitle",
    "cover_url",
    "kind",
    "status",
    "episodes_text",
    "year",
    "date_published",
    "source",
    "source_count",
    "available_episode_count",
    "is_favorite",
    "watched",
    "progress_episode_number",
    "watch_status",
    "not_interested",
    "state_updated_at",
    "favorite_updated_at",
    "watch_status_updated_at",
    "not_interested_updated_at",
    "watch_last_seen_at",
    "listing_score",
    "aggregate_score",
    "aggregate_count",
    "effective_score",
    "effective_score_source",
)


def keep_public_value(value):
    return value not in (None, "", False)


def catalog_api_item(item):
    payload = {
        key: item.get(key)
        for key in CATALOG_API_ITEM_FIELDS
        if keep_public_value(item.get(key))
    }
    # State fields are a stable API shape.  In particular, false/null values
    # carry meaning for optimistic UI reconciliation and must not be removed by
    # the generic compact-payload filter above.
    payload.update(
        {
            "is_favorite": bool(item.get("is_favorite")),
            "watched": bool(item.get("watched")),
            "progress_episode_number": item.get("progress_episode_number"),
            "watch_status": item.get("watch_status"),
            "not_interested": bool(item.get("not_interested")),
            "state_updated_at": item.get("state_updated_at"),
            "favorite_updated_at": item.get("favorite_updated_at"),
            "watch_status_updated_at": item.get("watch_status_updated_at"),
            "not_interested_updated_at": item.get("not_interested_updated_at"),
            "watch_last_seen_at": item.get("watch_last_seen_at"),
        }
    )
    genres = list(item.get("genres") or [])
    if genres:
        payload["genres"] = genres
    sources = list(item.get("sources") or [])
    if sources:
        payload["sources"] = sources
    variants = [
        compact_catalog_variant(variant)
        for variant in item.get("source_variants") or []
        if variant.get("id") != item.get("id")
    ]
    if variants:
        payload["source_variants"] = variants
    if item.get("recent_update_summary"):
        payload["recent_update_summary"] = dict(item["recent_update_summary"])
    return payload


def catalog_api_items(items):
    return [catalog_api_item(item) for item in items]


def clone_catalog_items(items, include_search_fields=False, user_state_by_source_id=None):
    return [
        clone_catalog_item(
            item,
            include_search_fields=include_search_fields,
            user_state_by_source_id=user_state_by_source_id,
        )
        for item in items
    ]


def catalog_item_with_search_fields(item, search_fields):
    merged = dict(item)
    merged["search_fields"] = [dict(field) for field in search_fields or []]
    return merged


def build_catalog_search_fields(cache, db_path=None):
    path = resolve_db_path(db_path)
    con = connect(path)
    try:
        fields_by_source_id = load_catalog_search_fields(con)
    finally:
        con.close()

    fields_by_item_id = {}
    for item in cache["items"]:
        fields = unique_structured_search_fields(
            field
            for source_id in item.get("source_member_ids") or [item.get("id")]
            for field in fields_by_source_id.get(source_id, [])
        )
        if fields:
            fields_by_item_id[item["id"]] = fields
    return fields_by_item_id


def catalog_search_fields(cache, db_path=None):
    fields = cache.get("search_fields_by_item_id")
    if fields is not None:
        return fields

    built = build_catalog_search_fields(cache, db_path)
    with CATALOG_CACHE_LOCK:
        return cache.setdefault("search_fields_by_item_id", built)


def get_anime_search_fields(db_path=None, user_id=None):
    cache = get_catalog_cache(db_path, user_id)
    fields = catalog_search_fields(cache, db_path)
    return [
        {
            "id": item["id"],
            "search_fields": [dict(field) for field in fields.get(item["id"]) or []],
        }
        for item in cache["items"]
        if fields.get(item["id"])
    ]


def catalog_revision_token(path, connection=None):
    """Return (schema version, catalog generation, dirty) without app init."""
    owns_connection = connection is None
    con = connection
    try:
        if con is None:
            con = sqlite3.connect(path, timeout=2)
            con.execute("pragma query_only=on")
        schema_version = con.execute("pragma schema_version").fetchone()[0]
        row = con.execute(
            "select generation, dirty from catalog_cache_revision where singleton = 1"
        ).fetchone()
        if not row:
            return None
        return schema_version, int(row[0]), int(row[1])
    except (OSError, sqlite3.Error):
        return None
    finally:
        if owns_connection and con is not None:
            con.close()


def mark_catalog_dirty(path):
    if not Path(path).is_file():
        return
    try:
        con = sqlite3.connect(path, timeout=2)
        try:
            con.execute("pragma busy_timeout=2000")
            con.execute(
                """
                update catalog_cache_revision
                set generation = generation + 1,
                    dirty = 1
                where singleton = 1 and dirty = 0
                """
            )
            con.commit()
        finally:
            con.close()
    except sqlite3.Error:
        # A not-yet-initialized/replaced database will install the marker when
        # the next cache build opens it.
        return


def prepare_catalog_cache_snapshot(path):
    con = connect(path)
    try:
        con.execute("begin immediate")
        ensure_catalog_revision_schema(con)
        con.execute(
            "update catalog_cache_revision set dirty = 0 where singleton = 1"
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def build_catalog_cache_snapshot(path):
    con = connect(path)
    try:
        con.execute("begin")
        items = canonicalize_items(get_source_anime_items(con, include_search_fields=False))
        attach_recent_updates(con, items)
        translation_rankings = build_translation_rankings(con)
    finally:
        con.close()

    id_map = {}
    slug_map = {}
    for item in items:
        slug_map[item["slug"]] = item
        slug_map[item["internal_id"]] = item
        for variant in item.get("source_variants") or []:
            id_map[int(variant["id"])] = item
            slug_map[canonical_slug_for_item(variant)] = item

    return {
        "items": items,
        "id_map": id_map,
        "slug_map": slug_map,
        "translation_rankings": translation_rankings,
    }


def build_catalog_cache(db_path=None):
    path = resolve_db_path(db_path)
    built = None
    # A writer racing a snapshot flips dirty back to one. Retry a bounded
    # number of times so ordinary cron commits are included immediately while
    # sustained writes cannot starve requests forever.
    for _ in range(3):
        prepare_catalog_cache_snapshot(path)
        built = build_catalog_cache_snapshot(path)
        token = catalog_revision_token(path)
        built["revision_token"] = token
        if token and token[2] == 0:
            break
    return built


def catalog_cache_is_current(path, cached, connection=None):
    current = catalog_revision_token(path, connection=connection)
    cached_token = cached.get("revision_token")
    return bool(
        current
        and cached_token
        and current[2] == 0
        and cached_token[2] == 0
        and current[:2] == cached_token[:2]
    )


def get_catalog_cache(db_path=None, user_id=None, connection=None):
    path = resolve_db_path(db_path)
    key = str(path.resolve())
    while True:
        with CATALOG_CACHE_LOCK:
            cached = CATALOG_CACHE.get(key)
        if cached and catalog_cache_is_current(path, cached, connection=connection):
            return cached

        with CATALOG_CACHE_LOCK:
            # Another thread may have replaced the entry while revision I/O
            # happened outside the global lock.
            current = CATALOG_CACHE.get(key)
            if current is not cached:
                continue
            build_event = CATALOG_CACHE_BUILDS.get(key)
            if build_event is None:
                build_event = threading.Event()
                CATALOG_CACHE_BUILDS[key] = build_event
                is_builder = True
            else:
                is_builder = False

        if not is_builder:
            build_event.wait()
            continue

        try:
            built = build_catalog_cache(path)
            with CATALOG_CACHE_LOCK:
                CATALOG_CACHE[key] = built
            return built
        finally:
            with CATALOG_CACHE_LOCK:
                CATALOG_CACHE_BUILDS.pop(key, None)
                build_event.set()


def invalidate_catalog_cache(db_path=None):
    path = resolve_db_path(db_path)
    prefix = str(path.resolve())
    mark_catalog_dirty(path)
    with CATALOG_CACHE_LOCK:
        for key in list(CATALOG_CACHE):
            if key == prefix or (isinstance(key, tuple) and key[0] == prefix):
                CATALOG_CACHE.pop(key, None)


def get_catalog_items(db_path=None, user_id=None):
    return get_catalog_cache(db_path, user_id)["items"]


def prewarm_catalog_cache(db_path=None):
    if not catalog_prewarm_enabled():
        return
    started = time.perf_counter()
    try:
        cache = get_catalog_cache(db_path)
    except Exception:
        server_logger().exception("catalog cache prewarm failed")
        return
    duration_ms = (time.perf_counter() - started) * 1000
    server_logger().info(
        "Catalog cache prewarmed in %.1f ms (%d items)",
        duration_ms,
        len(cache.get("items") or []),
    )


def canonical_group_for_anime_id(con, anime_id, user_id=None):
    db_path = con.execute("pragma database_list").fetchone()["file"]
    return get_catalog_cache(db_path, user_id, connection=con)["id_map"].get(int(anime_id))


def canonical_group_for_anime_ref(con, anime_ref, user_id=None):
    value = str(anime_ref or "").strip()
    if not value:
        return None
    if value.isdigit():
        return canonical_group_for_anime_id(con, int(value), user_id)
    db_path = con.execute("pragma database_list").fetchone()["file"]
    return get_catalog_cache(db_path, user_id, connection=con)["slug_map"].get(value)


def row_value(row, key, default=None):
    if isinstance(row, sqlite3.Row):
        return row[key] if key in row.keys() else default
    return row.get(key, default)


def aggregate_state_rows(rows):
    if not rows:
        return normalize_state(None)
    normalized_rows = []
    for index, row in enumerate(rows):
        normalized = normalize_state(
            {
                "is_favorite": row_value(row, "is_favorite", False),
                "progress_episode_number": row_value(row, "progress_episode_number"),
                "watched": row_value(row, "watched", False),
                "watch_status": row_value(row, "watch_status"),
                "not_interested": row_value(row, "not_interested", False),
                "updated_at": row_value(row, "updated_at", row_value(row, "state_updated_at")),
                "favorite_updated_at": row_value(row, "favorite_updated_at"),
                "watch_status_updated_at": row_value(row, "watch_status_updated_at"),
                "not_interested_updated_at": row_value(row, "not_interested_updated_at"),
            }
        )
        normalized["_index"] = index
        normalized["_anime_id"] = row_value(row, "anime_id", row_value(row, "id", 0)) or 0
        normalized_rows.append(normalized)

    def latest_for(field):
        candidates = [state for state in normalized_rows if state.get(field)]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda state: (
                state.get(field) or "",
                state.get("updated_at") or "",
                state.get("_anime_id") or 0,
                state["_index"],
            ),
        )

    favorite_row = latest_for("favorite_updated_at")
    not_interested_row = latest_for("not_interested_updated_at")
    watch_row = latest_for("watch_status_updated_at")

    if watch_row is None:
        progress_values = [
            state["progress_episode_number"]
            for state in normalized_rows
            if state["progress_episode_number"] is not None
        ]
        watched = any(state["watched"] for state in normalized_rows)
        progress = max(progress_values) if progress_values else None
        watch_status = "completed" if watched else ("watching" if progress is not None else "none")
    else:
        watched = watch_row["watched"]
        progress = watch_row["progress_episode_number"]
        watch_status = watch_row["watch_status"]

    state = {
        "is_favorite": favorite_row["is_favorite"] if favorite_row else any(
            value["is_favorite"] for value in normalized_rows
        ),
        "progress_episode_number": progress,
        "watched": watched,
        "watch_status": watch_status,
        "not_interested": not_interested_row["not_interested"] if not_interested_row else any(
            value["not_interested"] for value in normalized_rows
        ),
        "updated_at": max(
            (value["updated_at"] for value in normalized_rows if value["updated_at"]),
            default=None,
        ),
        "favorite_updated_at": max(
            (value["favorite_updated_at"] for value in normalized_rows if value["favorite_updated_at"]),
            default=None,
        ),
        "watch_status_updated_at": max(
            (value["watch_status_updated_at"] for value in normalized_rows if value["watch_status_updated_at"]),
            default=None,
        ),
        "not_interested_updated_at": max(
            (value["not_interested_updated_at"] for value in normalized_rows if value["not_interested_updated_at"]),
            default=None,
        ),
    }
    state = normalize_state(state)
    watch_engaged_seconds = sum(int(row_value(row, "watch_engaged_seconds", 0) or 0) for row in rows)
    meaningful_watch_seconds = sum(int(row_value(row, "meaningful_watch_seconds", 0) or 0) for row in rows)
    meaningful_watch_episode_count = sum(int(row_value(row, "meaningful_watch_episode_count", 0) or 0) for row in rows)
    watch_last_seen_at = max(
        (row_value(row, "watch_last_seen_at") for row in rows if row_value(row, "watch_last_seen_at")),
        default=None,
    )
    if watch_engaged_seconds or meaningful_watch_seconds or meaningful_watch_episode_count or watch_last_seen_at:
        state.update({
            "watch_engaged_seconds": watch_engaged_seconds,
            "meaningful_watch_seconds": meaningful_watch_seconds,
            "meaningful_watch_episode_count": meaningful_watch_episode_count,
            "watch_last_seen_at": watch_last_seen_at,
        })
    return state


def get_group_state(con, anime_ids, user_id=None):
    if not anime_ids:
        return normalize_state(None)
    user_id = resolved_user_id(con, user_id)
    if user_id is None:
        return normalize_state(None)
    rows = con.execute(
        f"""
        select *
        from user_title_state
        where user_id = ?
          and anime_id in ({sql_placeholders(anime_ids)})
        """,
        (user_id, *anime_ids),
    ).fetchall()
    return aggregate_state_rows(rows)


def get_group_title_navigation(con, anime_ids, user_id=None):
    if not anime_ids:
        return None
    user_id = resolved_user_id(con, user_id)
    if user_id is None:
        return None
    row = con.execute(
        f"""
        select episode_id, episode_number, updated_at
        from user_title_navigation_state
        where user_id = ?
          and anime_id in ({sql_placeholders(anime_ids)})
        order by updated_at desc, anime_id desc
        limit 1
        """,
        (user_id, *anime_ids),
    ).fetchone()
    return dict(row) if row else None


def update_title_navigation(anime_ref, episode_id, db_path=None, user_id=None):
    if type(episode_id) is not int or episode_id < 1:
        raise ValueError("episode_id must be a positive integer")
    con = connect(db_path)
    try:
        user_id = require_user_id(con, user_id)
        group = canonical_group_for_anime_ref(con, anime_ref, user_id)
        if not group:
            return None
        target_id = group["id"]
        member_ids = [variant["id"] for variant in group.get("source_variants") or []] or [target_id]
        con.execute("begin immediate")
        episode = con.execute(
            f"""
            select id, number
            from episodes
            where id = ?
              and anime_id in ({sql_placeholders(member_ids)})
            """,
            (episode_id, *member_ids),
        ).fetchone()
        if not episode:
            raise ValueError("episode_id is invalid for this title")
        timestamp = now_iso()
        con.execute(
            """
            insert into user_title_navigation_state (
                user_id, anime_id, episode_id, episode_number, updated_at
            ) values (?, ?, ?, ?, ?)
            on conflict(user_id, anime_id) do update set
                episode_id = excluded.episode_id,
                episode_number = excluded.episode_number,
                updated_at = excluded.updated_at
            """,
            (user_id, target_id, episode["id"], episode["number"], timestamp),
        )
        duplicate_ids = [item for item in member_ids if item != target_id]
        if duplicate_ids:
            con.execute(
                f"""
                delete from user_title_navigation_state
                where user_id = ?
                  and anime_id in ({sql_placeholders(duplicate_ids)})
                """,
                (user_id, *duplicate_ids),
            )
        con.commit()
        return {
            "episode_id": episode["id"],
            "episode_number": episode["number"],
            "updated_at": timestamp,
        }
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def episode_number_key(value, fallback):
    raw = str(value or "").strip()
    number = numeric(raw)
    if number is not None:
        return f"n:{int(number)}" if float(number).is_integer() else f"n:{number}"
    key = normalize_key(raw)
    return f"s:{key}" if key else f"id:{fallback}"


def episode_key(episode):
    return episode_number_key(episode.get("number"), episode.get("id"))


def episode_sort_key(episode):
    number = numeric(episode.get("number"))
    return (
        number is None,
        number if number is not None else normalize_key(episode.get("number")),
        source_priority(episode.get("anime_source")),
        episode.get("id") or 0,
    )


def provider_sort_key(source):
    provider = normalize_key(source.get("provider_title"))
    host = normalize_key(source.get("embed_host"))
    if provider.startswith("kodik"):
        priority = 0
    elif provider == "aniboom":
        priority = 1
    elif provider == "cvh":
        priority = 2
    elif provider == "sibnet":
        priority = 3
    else:
        priority = 50
    return (
        priority,
        source.get("provider_title") or "",
        host,
        source.get("id") or 0,
    )


def build_source_ranking_context(by_episode, translation_rankings):
    episode_counts_by_key = {}
    providers_by_key = {}

    for _episode_id, sources in by_episode.items():
        episode_translation_keys = set()
        for source in sources:
            key = translation_key(source.get("translation_title"))
            episode_translation_keys.add(key)
            providers_by_key.setdefault(key, set()).add(
                (
                    normalize_key(source.get("provider_title")),
                    normalize_key(source.get("embed_host")),
                )
            )
        for key in episode_translation_keys:
            episode_counts_by_key[key] = episode_counts_by_key.get(key, 0) + 1

    return {
        "translation_rankings": translation_rankings or {},
        "episode_counts_by_key": episode_counts_by_key,
        "providers_by_key": providers_by_key,
    }


def translation_sort_key(source, context=None):
    context = context or {}
    key = translation_key(source.get("translation_title"))
    ranking = context.get("translation_rankings", {}).get(key) or {}
    episode_count = context.get("episode_counts_by_key", {}).get(key, 0)
    provider_count = len(context.get("providers_by_key", {}).get(key, set()))

    return (
        0 if key in PINNED_TRANSLATION_KEYS else 1,
        1 if key in GENERIC_TRANSLATION_KEYS else 0,
        1 if key in SUBTITLE_TRANSLATION_KEYS else 0,
        -episode_count,
        ranking.get("rank", UNKNOWN_TRANSLATION_RANK),
        -provider_count,
        source.get("translation_title") or "",
    )


def source_row_sort_key(source, context=None):
    return (
        source.get("episode_id") or 0,
        *translation_sort_key(source, context),
        source_priority(source.get("source")),
        *provider_sort_key(source),
    )


def get_anime_detail(anime_ref, db_path=None, user_id=None):
    con = connect(db_path)
    user_id = resolved_user_id(con, user_id)
    group = canonical_group_for_anime_ref(con, anime_ref, user_id)
    if not group:
        con.close()
        return None

    member_ids = [variant["id"] for variant in group.get("source_variants") or []]
    primary_id = group["id"]
    member_sql = sql_placeholders(member_ids)

    anime = con.execute("select * from anime where id = ?", (primary_id,)).fetchone()
    if not anime:
        con.close()
        return None

    genres = rows_to_dicts(
        con.execute(
            f"""
            select distinct g.genre
            from anime_genres g
            join anime a on a.id = g.anime_id
            where g.anime_id in ({member_sql})
            order by case a.source when 'animego' then 0 when 'yummyanime' then 1 else 9 end, g.genre
            """,
            member_ids,
        ).fetchall()
    )
    dubbings = rows_to_dicts(
        con.execute(
            f"""
            select distinct d.dubbing
            from anime_dubbings d
            join anime a on a.id = d.anime_id
            where d.anime_id in ({member_sql})
            order by case a.source when 'animego' then 0 when 'yummyanime' then 1 else 9 end, d.dubbing
            """,
            member_ids,
        ).fetchall()
    )
    episode_rows = rows_to_dicts(
        con.execute(
            f"""
            select
                e.*,
                a.source as anime_source,
                a.source_id as anime_source_id,
                count(vs.id) as source_count
            from episodes e
            join anime a on a.id = e.anime_id
            left join video_sources vs on vs.episode_id = e.id and vs.embed_url is not null
            where e.anime_id in ({member_sql})
            group by e.id
            order by cast(e.number as integer), e.id
            """,
            member_ids,
        ).fetchall()
    )
    source_rows = rows_to_dicts(
        con.execute(
            f"""
            select
                vs.id,
                vs.anime_id as source_anime_id,
                vs.episode_id,
                vs.provider_id,
                vs.provider_title,
                vs.translation_id,
                vs.translation_title,
                vs.embed_host,
                vs.embed_url,
                vs.embed_url_redacted,
                a.source,
                a.source_id,
                e.number as episode_number
            from video_sources vs
            join anime a on a.id = vs.anime_id
            join episodes e on e.id = vs.episode_id
            where vs.anime_id in ({member_sql})
              and vs.embed_url is not null
            order by
                cast(e.number as integer),
                vs.episode_id,
                case a.source when 'animego' then 0 when 'yummyanime' then 1 else 9 end,
                vs.translation_title,
                case when lower(vs.provider_title) = 'kodik' then 0 else 1 end,
                vs.provider_title
            """,
            member_ids,
        ).fetchall()
    )
    fields = rows_to_dicts(
        con.execute(
            "select label, value from anime_fields where anime_id = ? order by label",
            (primary_id,),
        ).fetchall()
    )
    state = get_group_state(con, member_ids, user_id)
    db_file = con.execute("pragma database_list").fetchone()["file"]
    translation_rankings = get_catalog_cache(db_file, user_id).get("translation_rankings") or {}
    title_navigation = get_group_title_navigation(con, member_ids, user_id)
    latest_watch_row = None
    if user_id is not None and state.get("watch_status") == "watching":
        latest_watch_row = con.execute(
            f"""
            select *
            from user_episode_state
            where user_id = ?
              and anime_id in ({member_sql})
              and started_at is not null
            order by last_seen_at desc, last_confidence desc, updated_at desc
            limit 1
            """,
            (user_id, *member_ids),
        ).fetchone()
        latest_watch_row = dict(latest_watch_row) if latest_watch_row else None
    con.close()

    episode_buckets = {}
    for episode in episode_rows:
        episode_buckets.setdefault(episode_key(episode), []).append(episode)

    episodes = []
    episode_id_by_key = {}
    for key, bucket in sorted(episode_buckets.items(), key=lambda item: episode_sort_key(sorted(item[1], key=episode_sort_key)[0])):
        selected = sorted(
            bucket,
            key=lambda episode: (
                0 if episode.get("anime_id") == primary_id else 1,
                source_priority(episode.get("anime_source")),
                0 if (episode.get("source_count") or 0) > 0 else 1,
                episode.get("id") or 0,
            ),
        )[0]
        episode = dict(selected)
        episode["source_count"] = 0
        episode.pop("anime_source", None)
        episode.pop("anime_source_id", None)
        episodes.append(episode)
        episode_id_by_key[key] = episode["id"]

    by_episode = {}
    for source in source_rows:
        key = episode_number_key(source.get("episode_number"), source.get("episode_id"))
        canonical_episode_id = episode_id_by_key.get(key)
        if canonical_episode_id is None:
            continue
        source = dict(source)
        source["episode_id"] = canonical_episode_id
        source.pop("episode_number", None)
        by_episode.setdefault(canonical_episode_id, []).append(source)

    for episode in episodes:
        episode["source_count"] = len(by_episode.get(episode["id"], []))
    source_ranking_context = build_source_ranking_context(by_episode, translation_rankings)
    for sources in by_episode.values():
        sources.sort(key=lambda source: source_row_sort_key(source, source_ranking_context))

    detail = dict(anime)
    detail.update(state)
    detail["genres"] = [row["genre"] for row in genres]
    detail["dubbings"] = [row["dubbing"] for row in dubbings]
    detail["fields"] = fields
    detail["episodes"] = episodes
    detail["sources_by_episode"] = by_episode
    detail["source_variants"] = group.get("source_variants") or []
    detail["source_variant_count"] = group.get("source_variant_count") or 1
    detail["sources"] = group.get("sources") or [detail.get("source")]
    detail["source_member_ids"] = member_ids
    for field in ("external_score", "external_score_source", "synthetic_score", "effective_score", "effective_score_source"):
        detail[field] = group.get(field)
    detail["slug"] = group.get("slug")
    detail["internal_id"] = group.get("internal_id")
    detail["source_count"] = sum(len(sources) for sources in by_episode.values())
    detail["available_episode_count"] = sum(1 for episode in episodes if episode.get("source_count"))
    detail["recent_updates"] = [dict(update) for update in group.get("recent_updates") or []]
    detail["recent_update_summary"] = dict(group["recent_update_summary"]) if group.get("recent_update_summary") else None
    if title_navigation:
        detail["last_opened_episode"] = title_navigation
    if latest_watch_row:
        last_watch = detail_watch_target_from_episode_state(latest_watch_row, detail)
        if last_watch:
            detail["last_watch"] = last_watch
            if last_watch.get("progress_episode_number") is not None:
                detail["progress_episode_number"] = last_watch["progress_episode_number"]
    return detail


def validate_user_state_patch(patch):
    if not isinstance(patch, dict):
        raise ValueError("state patch must be an object")
    state_patch = {key: value for key, value in patch.items() if key != "video_source_id"}
    validated = user_state_model.validate_patch(state_patch)
    if "video_source_id" in patch:
        source_id = patch["video_source_id"]
        if source_id is not None and (type(source_id) is not int or source_id < 1):
            raise ValueError("video_source_id must be a positive integer or null")
        if "progress_episode_number" not in validated:
            raise ValueError("video_source_id requires progress_episode_number")
        if source_id is not None and validated["progress_episode_number"] is None:
            raise ValueError("video_source_id requires a non-null progress_episode_number")
        validated["video_source_id"] = source_id
    return validated


def update_user_state(anime_ref, patch, db_path=None, user_id=None):
    patch = validate_user_state_patch(patch)
    con = connect(db_path)
    try:
        user_id = require_user_id(con, user_id)
        group = canonical_group_for_anime_ref(con, anime_ref, user_id)
        if not group:
            return None

        target_id = group["id"]
        member_ids = [variant["id"] for variant in group.get("source_variants") or []] or [target_id]
        translation_rankings = None
        if "progress_episode_number" in patch:
            db_file = con.execute("pragma database_list").fetchone()["file"]
            translation_rankings = get_catalog_cache(
                db_file,
                user_id,
                connection=con,
            ).get("translation_rankings") or {}
        con.execute("begin immediate")
        current = get_group_state(con, member_ids, user_id)
        timestamp = now_iso()
        state_patch = {key: value for key, value in patch.items() if key != "video_source_id"}
        next_state = user_state_model.apply_patch(current, state_patch, timestamp)

        # Consolidation and the transition are serialized by BEGIN IMMEDIATE.
        # The model applies the patch to the freshly-read aggregate, so
        # independent concurrent field updates survive while derived legacy
        # fields (watched/progress) remain consistent with watch_status.
        con.execute(
            """
            insert into user_title_state (
                user_id, anime_id, is_favorite, progress_episode_number, watched,
                watch_status, not_interested, updated_at, favorite_updated_at,
                watch_status_updated_at, not_interested_updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(user_id, anime_id) do update set
                is_favorite = excluded.is_favorite,
                progress_episode_number = excluded.progress_episode_number,
                watched = excluded.watched,
                watch_status = excluded.watch_status,
                not_interested = excluded.not_interested,
                updated_at = excluded.updated_at,
                favorite_updated_at = excluded.favorite_updated_at,
                watch_status_updated_at = excluded.watch_status_updated_at,
                not_interested_updated_at = excluded.not_interested_updated_at
            """,
            (
                user_id,
                target_id,
                1 if next_state["is_favorite"] else 0,
                next_state["progress_episode_number"],
                1 if next_state["watched"] else 0,
                next_state["watch_status"],
                1 if next_state["not_interested"] else 0,
                next_state["updated_at"],
                next_state["favorite_updated_at"],
                next_state["watch_status_updated_at"],
                next_state["not_interested_updated_at"],
            ),
        )

        duplicate_state_ids = [item for item in member_ids if item != target_id]
        if duplicate_state_ids:
            con.execute(
                f"""
                delete from user_title_state
                where user_id = ? and anime_id in ({sql_placeholders(duplicate_state_ids)})
                """,
                (user_id, *duplicate_state_ids),
            )

        manual_last_watch = None
        sync_episode_state = "progress_episode_number" in patch or patch.get("watch_status") == "none"
        if sync_episode_state:
            manual_last_watch = sync_manual_progress_to_episode_state(
                con,
                group,
                user_id,
                next_state["progress_episode_number"],
                timestamp,
                translation_rankings=translation_rankings,
                selected_video_source_id=patch.get("video_source_id"),
            )
        row = con.execute(
            "select * from user_title_state where user_id = ? and anime_id = ?",
            (user_id, target_id),
        ).fetchone()
        next_state = normalize_state(row)
        if sync_episode_state:
            next_state["last_watch"] = manual_last_watch
        con.commit()
        return next_state
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def bounded_text(value, limit=200):
    if value in (None, ""):
        return None
    return str(value).strip()[:limit] or None


def int_from_value(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        match = re.search(r"\d+", str(value))
        return int(match.group(0)) if match else None


def nonnegative_int(value, default=0, maximum=None):
    parsed = int_from_value(value)
    if parsed is None:
        parsed = default
    parsed = max(0, parsed)
    return min(parsed, maximum) if maximum is not None else parsed


def bool_payload_value(value):
    if value is None:
        return None
    return 1 if bool(value) else 0


def optional_json_integer(payload, field, *, minimum=None, maximum=None):
    if field not in payload or payload[field] is None:
        return None
    value = payload[field]
    if type(value) is not int:
        raise ValueError(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    if maximum is not None and value > maximum:
        return maximum
    return value


def optional_json_boolean(payload, field):
    if field not in payload or payload[field] is None:
        return None
    if type(payload[field]) is not bool:
        raise ValueError(f"{field} must be a boolean")
    return 1 if payload[field] else 0


def require_payload_match(payload, field, expected):
    value = payload.get(field)
    if value in (None, ""):
        return
    if expected in (None, "") or str(value).strip() != str(expected).strip():
        raise ValueError(f"{field} does not match video_source_id")


def sanitize_watch_metadata(payload):
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    if not isinstance(metadata, dict):
        return "{}"
    cleaned = sanitize_client_error_value(metadata)
    encoded = json.dumps(cleaned, ensure_ascii=False, sort_keys=True)
    if len(encoded.encode("utf-8")) > MAX_WATCH_METADATA_BYTES:
        return json.dumps({"truncated": True}, ensure_ascii=False, sort_keys=True)
    return encoded


def watch_event_confidence(event_type, engaged_seconds=0, page_visible=None, player_focused=None):
    if event_type == "player_loaded":
        return 0.15
    if event_type in {"fullscreen_enter", "pip_open"}:
        return 0.95
    if event_type == "player_engaged":
        return 0.85
    if event_type in {"episode_selected", "source_changed"}:
        return 0.4
    if event_type == "heartbeat":
        if engaged_seconds <= 0:
            return 0.35
        if page_visible is False:
            return 0.35
        return 0.8 if player_focused else 0.7
    if event_type in {"page_hidden", "session_end"}:
        return 0.45 if engaged_seconds > 0 else 0.25
    return 0.0


def episode_progress_number(value):
    return int_from_value(value)


def load_event_episode(con, member_ids, payload):
    episode_id = optional_json_integer(payload, "episode_id", minimum=1)
    if episode_id is not None:
        row = con.execute(
            f"""
            select *
            from episodes
            where id = ?
              and anime_id in ({sql_placeholders(member_ids)})
            """,
            (episode_id, *member_ids),
        ).fetchone()
        if row:
            return row
        raise ValueError("episode_id is invalid for this title")

    progress_number = optional_json_integer(payload, "progress_episode_number", minimum=0)
    if progress_number is None:
        progress_number = episode_progress_number(payload.get("episode_number"))
    if progress_number is None:
        raise ValueError("episode_id or episode_number is required")
    row = con.execute(
        f"""
        select *
        from episodes
        where anime_id in ({sql_placeholders(member_ids)})
          and cast(number as integer) = ?
        order by anime_id, id
        limit 1
        """,
        (*member_ids, progress_number),
    ).fetchone()
    if not row:
        raise ValueError("episode_number is invalid for this title")
    return row


def load_event_video_source(con, member_ids, payload, episode):
    source_id = optional_json_integer(payload, "video_source_id", minimum=1)
    if source_id is None:
        return None
    row = con.execute(
        f"""
        select
            vs.*,
            a.source,
            a.source_id as catalog_source_id,
            e.number as source_episode_number
        from video_sources vs
        join anime a on a.id = vs.anime_id
        join episodes e on e.id = vs.episode_id
        where vs.id = ?
          and vs.anime_id in ({sql_placeholders(member_ids)})
        """,
        (source_id, *member_ids),
    ).fetchone()
    if not row:
        raise ValueError("video_source_id is invalid for this title")
    selected_number = episode_progress_number(episode["number"])
    source_number = episode_progress_number(row["source_episode_number"])
    if selected_number is not None and source_number != selected_number:
        raise ValueError("video_source_id is invalid for this episode")
    return row


def load_episode_for_progress(con, member_ids, progress_episode_number, target_id=None):
    if progress_episode_number is None:
        return None
    target_id = target_id if target_id is not None else member_ids[0]
    row = con.execute(
        f"""
        select
            e.*,
            a.source as anime_source,
            count(vs.id) as source_count
        from episodes e
        join anime a on a.id = e.anime_id
        left join video_sources vs on vs.episode_id = e.id and vs.embed_url is not null
        where e.anime_id in ({sql_placeholders(member_ids)})
          and cast(e.number as integer) = ?
        group by e.id
        order by
            case when e.anime_id = ? then 0 else 1 end,
            case a.source when 'animego' then 0 when 'yummyanime' then 1 else 9 end,
            case when count(vs.id) > 0 then 0 else 1 end,
            e.id
        limit 1
        """,
        (*member_ids, int(progress_episode_number), target_id),
    ).fetchone()
    return row


def load_preferred_video_source_for_progress(
    con,
    member_ids,
    progress_episode_number,
    selected_video_source_id=None,
    translation_rankings=None,
):
    rows = con.execute(
        f"""
        select
            vs.id,
            vs.anime_id as source_anime_id,
            vs.episode_id,
            vs.provider_id,
            vs.provider_title,
            vs.translation_id,
            vs.translation_title,
            vs.embed_host,
            a.source,
            a.source_id,
            e.number as episode_number
        from video_sources vs
        join anime a on a.id = vs.anime_id
        join episodes e on e.id = vs.episode_id
        where vs.anime_id in ({sql_placeholders(member_ids)})
          and vs.embed_url is not null
        """,
        member_ids,
    ).fetchall()

    by_episode = {}
    candidates = []
    for raw_row in rows:
        source = dict(raw_row)
        episode_bucket = episode_number_key(source.get("episode_number"), source.get("episode_id"))
        by_episode.setdefault(episode_bucket, []).append(source)
        if episode_progress_number(source.get("episode_number")) == int(progress_episode_number):
            candidates.append(source)

    if not candidates:
        return None

    if selected_video_source_id is not None:
        selected = next(
            (source for source in candidates if str(source.get("id")) == str(selected_video_source_id)),
            None,
        )
        if selected:
            return selected

    # Detail view canonicalizes source rows into a shared episode bucket before
    # applying source_row_sort_key. Mirror that here so a manual progress PATCH
    # gets exactly the same fallback source as the player UI.
    for sources in by_episode.values():
        for source in sources:
            source["episode_id"] = 0
    if translation_rankings is None:
        translation_rankings = build_translation_rankings(con)
    context = build_source_ranking_context(by_episode, translation_rankings)
    return min(candidates, key=lambda source: source_row_sort_key(source, context))


def watch_target_from_episode_source(episode, source, progress_episode_number, timestamp, engaged_seconds=0):
    if not episode:
        return None
    source = dict(source) if source else {}
    return {
        "episode_id": episode["id"],
        "episode_number": episode["number"],
        "progress_episode_number": progress_episode_number,
        "source": source.get("source"),
        "translation_id": source.get("translation_id"),
        "provider_id": source.get("provider_id"),
        "video_source_id": source.get("id"),
        "last_seen_at": timestamp,
        "engaged_seconds": engaged_seconds,
    }


def apply_watch_progress_to_user_state(
    con,
    group,
    user_id,
    progress_episode_number,
    timestamp,
    event_type=None,
):
    if progress_episode_number is None:
        return get_group_state(con, [variant["id"] for variant in group.get("source_variants") or []], user_id)

    target_id = group["id"]
    member_ids = [variant["id"] for variant in group.get("source_variants") or []]
    current = get_group_state(con, member_ids, user_id)
    explicit_resume = watch_event_is_explicit_resume(event_type)
    if not watch_event_can_start_title(current, event_type):
        # A heartbeat already in flight when the user changes a shelf/status
        # must not undo that explicit decision.  The episode telemetry is still
        # retained, but only a fresh direct player action resumes the title.
        return current
    next_state = user_state_model.apply_patch(
        current,
        {
            "progress_episode_number": max(0, int(progress_episode_number)),
            "watch_status": "watching",
            **({"not_interested": False} if explicit_resume else {}),
        },
        timestamp,
    )
    con.execute(
        """
        insert into user_title_state (
            user_id,
            anime_id,
            is_favorite,
            progress_episode_number,
            watched,
            watch_status,
            not_interested,
            updated_at,
            favorite_updated_at,
            watch_status_updated_at,
            not_interested_updated_at
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(user_id, anime_id) do update set
            is_favorite = excluded.is_favorite,
            progress_episode_number = excluded.progress_episode_number,
            watched = excluded.watched,
            watch_status = excluded.watch_status,
            not_interested = excluded.not_interested,
            updated_at = excluded.updated_at,
            favorite_updated_at = excluded.favorite_updated_at,
            watch_status_updated_at = excluded.watch_status_updated_at,
            not_interested_updated_at = excluded.not_interested_updated_at
        """,
        (
            user_id,
            target_id,
            1 if next_state["is_favorite"] else 0,
            next_state["progress_episode_number"],
            1 if next_state["watched"] else 0,
            next_state["watch_status"],
            1 if next_state["not_interested"] else 0,
            next_state["updated_at"],
            next_state["favorite_updated_at"],
            next_state["watch_status_updated_at"],
            next_state["not_interested_updated_at"],
        ),
    )
    duplicate_state_ids = [item for item in member_ids if item != target_id]
    if duplicate_state_ids:
        con.execute(
            f"""
            delete from user_title_state
            where user_id = ?
              and anime_id in ({sql_placeholders(duplicate_state_ids)})
            """,
            (user_id, *duplicate_state_ids),
        )
    return next_state


def watch_event_is_explicit_resume(event_type):
    return event_type == "player_engaged"


def watch_event_can_start_title(current, event_type):
    if current.get("watch_status") == "completed":
        return False
    if current.get("watch_status") == "none" or current.get("not_interested"):
        return watch_event_is_explicit_resume(event_type)
    return True


def sync_manual_progress_to_episode_state(
    con,
    group,
    user_id,
    progress_episode_number,
    timestamp,
    translation_rankings=None,
    selected_video_source_id=None,
):
    target_id = group["id"]
    member_ids = [variant["id"] for variant in group.get("source_variants") or []]
    if not member_ids:
        member_ids = [target_id]

    if progress_episode_number is None:
        con.execute(
            f"""
            update user_episode_state
            set started_at = null,
                last_event_type = 'manual_clear',
                last_confidence = 1.0,
                updated_at = ?
            where user_id = ?
              and anime_id in ({sql_placeholders(member_ids)})
            """,
            (timestamp, user_id, *member_ids),
        )
        return None

    episode = load_episode_for_progress(con, member_ids, progress_episode_number, target_id=target_id)
    if not episode:
        return None

    existing = con.execute(
        f"""
        select *
        from user_episode_state
        where user_id = ?
          and anime_id in ({sql_placeholders(member_ids)})
          and progress_episode_number = ?
        order by
            case
                when video_source_id is not null
                 and last_event_type not in ('manual_progress', 'manual_clear')
                then 0 else 1
            end,
            last_seen_at desc,
            last_confidence desc,
            updated_at desc
        limit 1
        """,
        (user_id, *member_ids, progress_episode_number),
    ).fetchone()
    source = load_preferred_video_source_for_progress(
        con,
        member_ids,
        progress_episode_number,
        selected_video_source_id=(
            selected_video_source_id
            if selected_video_source_id is not None
            else (existing["video_source_id"] if existing else None)
        ),
        translation_rankings=translation_rankings,
    )
    if selected_video_source_id is not None and (
        source is None or int(source["id"]) != int(selected_video_source_id)
    ):
        raise ValueError("video_source_id is invalid for this title and episode")
    engaged_seconds = int(existing["engaged_seconds"] or 0) if existing else 0
    upsert_episode_watch_state(
        con,
        user_id=user_id,
        anime_id=target_id,
        episode_id=episode["id"],
        timestamp=timestamp,
        event_type="manual_progress",
        confidence=1.0,
        engaged_seconds=0,
        heartbeat_count=0,
        started=True,
        episode_number=episode["number"],
        progress_episode_number=progress_episode_number,
        video_source_id=source["id"] if source else None,
        source=source["source"] if source else None,
        source_anime_id=source["source_anime_id"] if source else None,
        translation_id=source["translation_id"] if source else None,
        translation_title=source["translation_title"] if source else None,
        provider_id=source["provider_id"] if source else None,
        provider_title=source["provider_title"] if source else None,
        embed_host=source["embed_host"] if source else None,
    )
    return watch_target_from_episode_source(
        episode,
        source,
        progress_episode_number,
        timestamp,
        engaged_seconds=engaged_seconds,
    )


def mark_previous_episode_completed(con, user_id, anime_id, progress_episode_number, timestamp):
    if progress_episode_number is None:
        return None
    previous = con.execute(
        """
        select *
        from user_episode_state
        where user_id = ?
          and anime_id = ?
          and progress_episode_number is not null
          and progress_episode_number < ?
          and completed_at is null
          and started_at is not null
          and engaged_seconds >= ?
        order by progress_episode_number desc, last_seen_at desc
        limit 1
        """,
        (user_id, anime_id, progress_episode_number, WATCH_NEXT_EPISODE_COMPLETION_SECONDS),
    ).fetchone()
    if not previous:
        return None
    con.execute(
        """
        update user_episode_state
        set completed_at = ?,
            completion_confidence = ?,
            updated_at = ?
        where user_id = ?
          and anime_id = ?
          and episode_id = ?
          and completed_at is null
        """,
        (timestamp, 0.8, timestamp, user_id, anime_id, previous["episode_id"]),
    )
    return dict(previous)


def upsert_episode_watch_state(
    con,
    *,
    user_id,
    anime_id,
    episode_id,
    timestamp,
    event_type,
    confidence,
    engaged_seconds,
    heartbeat_count,
    started,
    episode_number,
    progress_episode_number,
    video_source_id,
    source,
    source_anime_id,
    translation_id,
    translation_title,
    provider_id,
    provider_title,
    embed_host,
):
    existing = con.execute(
        """
        select *
        from user_episode_state
        where user_id = ?
          and anime_id = ?
          and episode_id = ?
        """,
        (user_id, anime_id, episode_id),
    ).fetchone()
    total_engaged = nonnegative_int(engaged_seconds)
    if existing:
        total_engaged += int(existing["engaged_seconds"] or 0)
    started_at = existing["started_at"] if existing and existing["started_at"] else (timestamp if started else None)
    completed_at = existing["completed_at"] if existing and existing["completed_at"] else None
    completion_confidence = existing["completion_confidence"] if existing and existing["completion_confidence"] else None
    if started_at and not completed_at and total_engaged >= WATCH_LIKELY_COMPLETED_SECONDS:
        completed_at = timestamp
        completion_confidence = 0.7

    next_row = {
        "user_id": user_id,
        "anime_id": anime_id,
        "episode_id": episode_id,
        "episode_number": episode_number,
        "progress_episode_number": progress_episode_number,
        "video_source_id": video_source_id,
        "source": source,
        "source_anime_id": source_anime_id,
        "translation_id": translation_id,
        "translation_title": translation_title,
        "provider_id": provider_id,
        "provider_title": provider_title,
        "embed_host": embed_host,
        "first_seen_at": existing["first_seen_at"] if existing else timestamp,
        "last_seen_at": timestamp,
        "started_at": started_at,
        "completed_at": completed_at,
        "engaged_seconds": total_engaged,
        "heartbeat_count": (int(existing["heartbeat_count"] or 0) if existing else 0) + heartbeat_count,
        "last_event_type": event_type,
        "last_confidence": confidence,
        "completion_confidence": completion_confidence,
        "updated_at": timestamp,
    }
    con.execute(
        """
        insert into user_episode_state (
            user_id,
            anime_id,
            episode_id,
            episode_number,
            progress_episode_number,
            video_source_id,
            source,
            source_anime_id,
            translation_id,
            translation_title,
            provider_id,
            provider_title,
            embed_host,
            first_seen_at,
            last_seen_at,
            started_at,
            completed_at,
            engaged_seconds,
            heartbeat_count,
            last_event_type,
            last_confidence,
            completion_confidence,
            updated_at
        )
        values (
            :user_id,
            :anime_id,
            :episode_id,
            :episode_number,
            :progress_episode_number,
            :video_source_id,
            :source,
            :source_anime_id,
            :translation_id,
            :translation_title,
            :provider_id,
            :provider_title,
            :embed_host,
            :first_seen_at,
            :last_seen_at,
            :started_at,
            :completed_at,
            :engaged_seconds,
            :heartbeat_count,
            :last_event_type,
            :last_confidence,
            :completion_confidence,
            :updated_at
        )
        on conflict(user_id, anime_id, episode_id) do update set
            episode_number = coalesce(excluded.episode_number, user_episode_state.episode_number),
            progress_episode_number = coalesce(excluded.progress_episode_number, user_episode_state.progress_episode_number),
            video_source_id = coalesce(excluded.video_source_id, user_episode_state.video_source_id),
            source = coalesce(excluded.source, user_episode_state.source),
            source_anime_id = coalesce(excluded.source_anime_id, user_episode_state.source_anime_id),
            translation_id = coalesce(excluded.translation_id, user_episode_state.translation_id),
            translation_title = coalesce(excluded.translation_title, user_episode_state.translation_title),
            provider_id = coalesce(excluded.provider_id, user_episode_state.provider_id),
            provider_title = coalesce(excluded.provider_title, user_episode_state.provider_title),
            embed_host = coalesce(excluded.embed_host, user_episode_state.embed_host),
            last_seen_at = excluded.last_seen_at,
            started_at = coalesce(user_episode_state.started_at, excluded.started_at),
            completed_at = coalesce(user_episode_state.completed_at, excluded.completed_at),
            engaged_seconds = excluded.engaged_seconds,
            heartbeat_count = excluded.heartbeat_count,
            last_event_type = excluded.last_event_type,
            last_confidence = excluded.last_confidence,
            completion_confidence = coalesce(user_episode_state.completion_confidence, excluded.completion_confidence),
            updated_at = excluded.updated_at
        """,
        next_row,
    )
    return next_row


def record_watch_event(payload, db_path=None, user_id=None):
    if not isinstance(payload, dict):
        raise ValueError("watch event payload must be an object")

    event_type = bounded_text(payload.get("event_type"), 40)
    if event_type not in WATCH_EVENT_TYPES:
        raise ValueError("unsupported watch event type")

    client_session_id = bounded_text(payload.get("client_session_id"), 120)
    if not client_session_id:
        raise ValueError("client_session_id is required")

    con = connect(db_path)
    try:
        user_id = require_user_id(con, user_id)
        group = canonical_group_for_anime_ref(con, payload.get("anime_id"), user_id)
        if not group:
            raise ValueError("anime_id is invalid")
        # Serialize the episode/source read and both progress writes with manual
        # PATCH updates. Whichever request arrives last sees the complete prior
        # state instead of racing on a stale user_episode_state row.
        con.execute("begin immediate")
        anime_id = group["id"]
        member_ids = [variant["id"] for variant in group.get("source_variants") or []]
        episode = load_event_episode(con, member_ids, payload)
        video_source = load_event_video_source(con, member_ids, payload, episode)
        timestamp = now_iso()
        engaged_seconds = optional_json_integer(
            payload,
            "engaged_seconds",
            minimum=0,
            maximum=MAX_WATCH_EVENT_ENGAGED_SECONDS,
        )
        engaged_seconds = engaged_seconds or 0
        page_visible = optional_json_boolean(payload, "page_visible")
        player_focused = optional_json_boolean(payload, "player_focused")
        if event_type == "heartbeat" and not (page_visible == 1 and player_focused == 1):
            engaged_seconds = 0
        confidence = watch_event_confidence(
            event_type,
            engaged_seconds,
            page_visible=bool(page_visible) if page_visible is not None else None,
            player_focused=bool(player_focused) if player_focused is not None else None,
        )

        actual_progress_number = episode_progress_number(episode["number"])
        supplied_episode_number = payload.get("episode_number")
        if supplied_episode_number not in (None, ""):
            supplied_progress_number = episode_progress_number(supplied_episode_number)
            if actual_progress_number is not None and supplied_progress_number != actual_progress_number:
                raise ValueError("episode_number does not match episode_id")
        episode_number = bounded_text(episode["number"], 40)
        progress_episode_number = optional_json_integer(
            payload,
            "progress_episode_number",
            minimum=0,
        )
        if progress_episode_number is None:
            progress_episode_number = actual_progress_number
        elif actual_progress_number is not None and progress_episode_number != actual_progress_number:
            raise ValueError("progress_episode_number does not match episode_id")

        source_anime_id = optional_json_integer(payload, "source_anime_id", minimum=1)
        if video_source:
            require_payload_match(payload, "source_anime_id", video_source["anime_id"])
            require_payload_match(payload, "source", video_source["source"])
            require_payload_match(payload, "translation_id", video_source["translation_id"])
            require_payload_match(payload, "translation_title", video_source["translation_title"])
            require_payload_match(payload, "provider_id", video_source["provider_id"])
            require_payload_match(payload, "provider_title", video_source["provider_title"])
            require_payload_match(payload, "embed_host", video_source["embed_host"])
            source_anime_id = video_source["anime_id"]
        elif source_anime_id is not None and source_anime_id not in member_ids:
            raise ValueError("source_anime_id is invalid for this title")
        source = bounded_text(video_source["source"] if video_source else payload.get("source"), 40)
        translation_id = bounded_text(
            video_source["translation_id"] if video_source else payload.get("translation_id"),
            80,
        )
        translation_title = bounded_text(
            video_source["translation_title"] if video_source else payload.get("translation_title"),
            200,
        )
        provider_id = bounded_text(
            video_source["provider_id"] if video_source else payload.get("provider_id"),
            80,
        )
        provider_title = bounded_text(
            video_source["provider_title"] if video_source else payload.get("provider_title"),
            200,
        )
        embed_host = bounded_text(video_source["embed_host"] if video_source else payload.get("embed_host"), 200)

        cur = con.execute(
            """
            insert into user_watch_events (
                user_id,
                anime_id,
                episode_id,
                video_source_id,
                client_session_id,
                event_type,
                event_at,
                episode_number,
                progress_episode_number,
                source,
                source_anime_id,
                translation_id,
                translation_title,
                provider_id,
                provider_title,
                embed_host,
                engaged_seconds,
                page_visible,
                player_focused,
                confidence,
                metadata_json,
                created_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                anime_id,
                episode["id"],
                video_source["id"] if video_source else None,
                client_session_id,
                event_type,
                timestamp,
                episode_number,
                progress_episode_number,
                source,
                source_anime_id,
                translation_id,
                translation_title,
                provider_id,
                provider_title,
                embed_host,
                engaged_seconds,
                page_visible,
                player_focused,
                confidence,
                sanitize_watch_metadata(payload),
                timestamp,
            ),
        )
        started = event_type in WATCH_PROGRESS_EVENT_TYPES and confidence >= WATCH_STARTED_CONFIDENCE
        if started:
            current_title_state = get_group_state(con, member_ids, user_id)
            event_state_is_current = True
            if "library_watch_status" in payload:
                expected_status = bounded_text(payload.get("library_watch_status"), 40)
                if expected_status not in user_state_model.WATCH_STATUS_SET:
                    raise ValueError("library_watch_status is invalid")
                event_state_is_current = expected_status == current_title_state.get("watch_status")
            if event_state_is_current and "library_watch_status_updated_at" in payload:
                expected_updated_at = bounded_text(payload.get("library_watch_status_updated_at"), 80)
                event_state_is_current = expected_updated_at == current_title_state.get("watch_status_updated_at")
            started = event_state_is_current and watch_event_can_start_title(current_title_state, event_type)
        episode_state = upsert_episode_watch_state(
            con,
            user_id=user_id,
            anime_id=anime_id,
            episode_id=episode["id"],
            timestamp=timestamp,
            event_type=event_type,
            confidence=confidence,
            engaged_seconds=engaged_seconds,
            heartbeat_count=1 if event_type == "heartbeat" else 0,
            started=started,
            episode_number=episode_number,
            progress_episode_number=progress_episode_number,
            video_source_id=video_source["id"] if video_source else None,
            source=source,
            source_anime_id=source_anime_id,
            translation_id=translation_id,
            translation_title=translation_title,
            provider_id=provider_id,
            provider_title=provider_title,
            embed_host=embed_host,
        )

        title_state = None
        if started:
            mark_previous_episode_completed(con, user_id, anime_id, progress_episode_number, timestamp)
            title_state = apply_watch_progress_to_user_state(
                con,
                group,
                user_id,
                progress_episode_number,
                timestamp,
                event_type=event_type,
            )
        con.commit()
        if title_state is None:
            title_state = get_group_state(con, member_ids, user_id)
        total_engaged_seconds = int(episode_state.get("engaged_seconds") or 0)
        previous_engaged_seconds = max(0, total_engaged_seconds - engaged_seconds)
        recommendation_signal_changed = (
            previous_engaged_seconds < MEANINGFUL_WATCH_SECONDS <= total_engaged_seconds
        )
        return {
            "ok": True,
            "event": {
                "id": cur.lastrowid,
                "event_type": event_type,
                "confidence": confidence,
                "engaged_seconds": engaged_seconds,
                "event_at": timestamp,
            },
            "episode_state": {
                key: episode_state.get(key)
                for key in (
                    "anime_id",
                    "episode_id",
                    "episode_number",
                    "progress_episode_number",
                    "started_at",
                    "completed_at",
                    "engaged_seconds",
                    "last_seen_at",
                )
            },
            "state": title_state,
            "recommendation_signal_changed": recommendation_signal_changed,
        }
    finally:
        con.close()


def source_for_continue_target(detail, episode_id, row):
    sources = detail.get("sources_by_episode", {}).get(str(episode_id))
    if sources is None:
        sources = detail.get("sources_by_episode", {}).get(episode_id, [])
    if not sources:
        return None

    video_source_id = row.get("video_source_id")
    if video_source_id is not None:
        for source in sources:
            if str(source.get("id")) == str(video_source_id):
                return source

    translation_id = row.get("translation_id")
    provider_id = row.get("provider_id")
    if translation_id is not None or provider_id is not None:
        for source in sources:
            translation_match = translation_id is None or str(source.get("translation_id")) == str(translation_id)
            provider_match = provider_id is None or str(source.get("provider_id")) == str(provider_id)
            if translation_match and provider_match:
                return source

    return sources[0]


def detail_watch_target_from_episode_state(row, detail):
    episodes = detail.get("episodes") or []
    current = next((episode for episode in episodes if str(episode.get("id")) == str(row.get("episode_id"))), None)
    if not current and row.get("progress_episode_number") is not None:
        current = next(
            (
                episode
                for episode in episodes
                if episode_progress_number(episode.get("number")) == row.get("progress_episode_number")
            ),
            None,
        )
    if not current:
        return None

    selected_source = source_for_continue_target(detail, current["id"], row)
    return {
        "episode_id": current["id"],
        "episode_number": current.get("number"),
        "progress_episode_number": row.get("progress_episode_number"),
        "source": selected_source.get("source") if selected_source else row.get("source"),
        "translation_id": selected_source.get("translation_id") if selected_source else row.get("translation_id"),
        "provider_id": selected_source.get("provider_id") if selected_source else row.get("provider_id"),
        "video_source_id": selected_source.get("id") if selected_source else row.get("video_source_id"),
        "last_seen_at": row.get("last_seen_at"),
        "engaged_seconds": row.get("engaged_seconds"),
    }


def next_available_episode(episodes, progress_episode_number):
    if progress_episode_number is None:
        return None
    candidates = [
        episode
        for episode in episodes
        if (episode.get("source_count") or 0) > 0
        and (episode_progress_number(episode.get("number")) or -1) > progress_episode_number
    ]
    return candidates[0] if candidates else None


def continue_target_from_episode_state(row, db_path=None, user_id=None, detail=None):
    detail = detail or get_anime_detail(row["anime_id"], db_path, user_id)
    if not detail:
        return None
    if detail.get("not_interested") or detail.get("watch_status") != "watching":
        return None

    episodes = detail.get("episodes") or []
    current = next((episode for episode in episodes if str(episode.get("id")) == str(row["episode_id"])), None)
    if not current and row["progress_episode_number"] is not None:
        current = next(
            (
                episode
                for episode in episodes
                if episode_progress_number(episode.get("number")) == row["progress_episode_number"]
            ),
            None,
        )
    if not current:
        return None

    target = current
    reason = "resume"
    if row["completed_at"]:
        follow_up = next_available_episode(episodes, row["progress_episode_number"])
        if follow_up:
            target = follow_up
            reason = "next_episode"
        else:
            reason = "latest_completed"

    selected_source = source_for_continue_target(detail, target["id"], row)
    if not selected_source:
        return None

    return {
        "anime_id": detail["id"],
        "anime_ref": detail.get("slug") or detail.get("internal_id") or detail["id"],
        "title": detail.get("title"),
        "cover_url": detail.get("cover_url"),
        "episode_id": target["id"],
        "episode_number": target.get("number"),
        "source": selected_source.get("source"),
        "translation_id": selected_source.get("translation_id"),
        "provider_id": selected_source.get("provider_id"),
        "video_source_id": selected_source.get("id"),
        "reason": reason,
        "last_seen_at": row["last_seen_at"],
        "completed_at": row["completed_at"],
        "engaged_seconds": row["engaged_seconds"],
    }


def get_continue_watching(db_path=None, user_id=None):
    con = connect(db_path)
    try:
        user_id = require_user_id(con, user_id)
        rows = con.execute(
            """
            select *
            from user_episode_state
            where user_id = ?
              and started_at is not null
            order by last_seen_at desc, last_confidence desc, updated_at desc
            """,
            (user_id,),
        ).fetchall()
    finally:
        con.close()

    catalog_items = get_anime_list(db_path, user_id=user_id)
    eligible_group_by_member_id = {}
    for item in catalog_items:
        if item.get("not_interested") or item.get("watch_status") != "watching":
            continue
        member_ids = item.get("source_member_ids") or [item.get("id")]
        for member_id in member_ids:
            if member_id is not None:
                eligible_group_by_member_id[int(member_id)] = item["id"]

    rows_by_group = OrderedDict()
    for row in rows:
        group_id = eligible_group_by_member_id.get(int(row["anime_id"]))
        if group_id is not None:
            rows_by_group.setdefault(group_id, []).append(dict(row))

    for group_id, group_rows in rows_by_group.items():
        detail = get_anime_detail(group_id, db_path, user_id)
        if not detail:
            continue
        for row in group_rows:
            target = continue_target_from_episode_state(
                row,
                db_path,
                user_id,
                detail=detail,
            )
            if target:
                return {"item": target}
    return {"item": None}


def public_user(row):
    if row is None:
        return None
    user = {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"] or row["email"] or "Google user",
        "picture_url": row["picture_url"],
    }
    user["is_admin"] = is_admin_user(user)
    return user


def session_token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def google_client_id():
    return os.environ.get("GOOGLE_CLIENT_ID", "").strip()


def session_cookie_secure():
    return os.environ.get("ANIME_SESSION_SECURE", "").strip().lower() in {"1", "true", "yes"}


def configured_admin_email():
    return os.environ.get("ANIME_ADMIN_EMAIL", "").strip().lower()


def is_admin_user(user):
    if not user:
        return False
    admin_email = configured_admin_email()
    if not admin_email:
        return False
    return str(user.get("email") or "").strip().lower() == admin_email


def verify_google_credential(credential):
    client_id = google_client_id()
    if not client_id:
        raise AuthConfigError("GOOGLE_CLIENT_ID is not configured")
    if not credential:
        raise AuthError("missing Google credential")

    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token
    except ImportError as exc:
        raise AuthConfigError("google-auth dependencies are not installed") from exc

    try:
        payload = id_token.verify_oauth2_token(
            credential,
            google_requests.Request(),
            client_id,
        )
    except ValueError as exc:
        raise AuthError("invalid Google credential") from exc

    if payload.get("iss") not in GOOGLE_ISSUERS:
        raise AuthError("invalid Google issuer")
    if not payload.get("sub"):
        raise AuthError("Google credential has no subject")
    if not payload.get("email_verified"):
        raise AuthError("Google email is not verified")

    email = str(payload.get("email") or "").strip().lower()
    allowed_emails = env_list("ANIME_AUTH_ALLOWED_EMAILS")
    if allowed_emails and email not in allowed_emails:
        raise AuthError("this Google account is not allowed")

    allowed_domains = env_list("ANIME_AUTH_ALLOWED_DOMAINS")
    if allowed_domains:
        domain = str(payload.get("hd") or email.rsplit("@", 1)[-1]).strip().lower()
        if domain not in allowed_domains:
            raise AuthError("this Google domain is not allowed")

    return payload


def upsert_google_user(con, profile):
    now = now_iso()
    google_sub = str(profile["sub"])
    email = str(profile.get("email") or "").strip().lower() or None
    name = profile.get("name") or email or "Google user"
    picture_url = profile.get("picture")
    con.execute(
        """
        insert into users (
            google_sub,
            email,
            email_verified,
            name,
            picture_url,
            created_at,
            last_login_at
        )
        values (?, ?, ?, ?, ?, ?, ?)
        on conflict(google_sub) do update set
            email = excluded.email,
            email_verified = excluded.email_verified,
            name = excluded.name,
            picture_url = excluded.picture_url,
            last_login_at = excluded.last_login_at
        """,
        (
            google_sub,
            email,
            1 if profile.get("email_verified") else 0,
            name,
            picture_url,
            now,
            now,
        ),
    )
    return con.execute(
        "select * from users where google_sub = ?",
        (google_sub,),
    ).fetchone()


def create_session(con, user_id):
    token = secrets.token_urlsafe(32)
    now = now_iso()
    expires_at = (
        dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=SESSION_TTL_SECONDS)
    ).isoformat(timespec="seconds")
    con.execute(
        """
        insert into sessions (token_hash, user_id, created_at, expires_at, revoked_at, last_seen_at)
        values (?, ?, ?, ?, null, ?)
        """,
        (session_token_hash(token), user_id, now, expires_at, now),
    )
    return token, expires_at


def session_user_is_allowed(row):
    email = str(row["email"] or "").strip().lower()
    allowed_emails = env_list("ANIME_AUTH_ALLOWED_EMAILS")
    if allowed_emails and email not in allowed_emails:
        return False
    allowed_domains = env_list("ANIME_AUTH_ALLOWED_DOMAINS")
    if allowed_domains:
        domain = email.rsplit("@", 1)[-1] if "@" in email else ""
        if domain not in allowed_domains:
            return False
    return True


def authenticate_google_credential(credential, db_path=None):
    profile = verify_google_credential(credential)
    con = connect(db_path)
    try:
        user = upsert_google_user(con, profile)
        token, expires_at = create_session(con, user["id"])
        con.commit()
        return {
            "user": public_user(user),
            "token": token,
            "expires_at": expires_at,
        }
    finally:
        con.close()


def get_session_user(token, db_path=None):
    if not token:
        return None
    con = connect(db_path)
    try:
        token_hash = session_token_hash(token)
        now = now_iso()
        row = con.execute(
            """
            select u.*, s.last_seen_at as session_last_seen_at
            from sessions s
            join users u on u.id = s.user_id
            where s.token_hash = ?
              and s.revoked_at is null
              and s.expires_at > ?
            """,
            (token_hash, now),
        ).fetchone()
        if row:
            if not session_user_is_allowed(row):
                con.execute(
                    "update sessions set revoked_at = ? where token_hash = ? and revoked_at is null",
                    (now, token_hash),
                )
                con.commit()
                return None
            cutoff = (
                dt.datetime.now(dt.timezone.utc)
                - dt.timedelta(seconds=SESSION_LAST_SEEN_WRITE_INTERVAL_SECONDS)
            ).isoformat(timespec="seconds")
            if not row["session_last_seen_at"] or row["session_last_seen_at"] < cutoff:
                con.execute(
                    """
                    update sessions
                    set last_seen_at = ?
                    where token_hash = ?
                      and (last_seen_at is null or last_seen_at < ?)
                    """,
                    (now, token_hash, cutoff),
                )
                con.commit()
        return public_user(row)
    finally:
        con.close()


def revoke_session(token, db_path=None):
    if not token:
        return
    con = connect(db_path)
    try:
        con.execute(
            """
            update sessions
            set revoked_at = ?
            where token_hash = ?
              and revoked_at is null
            """,
            (now_iso(), session_token_hash(token)),
        )
        con.commit()
    finally:
        con.close()


def admin_users_payload(db_path=None):
    now = now_iso()
    recent_cutoff = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)
    ).isoformat(timespec="seconds")
    con = connect(db_path)
    try:
        users = rows_to_dicts(
            con.execute(
                """
                with state_stats as (
                    select
                        user_id,
                        count(*) as touched_titles,
                        sum(case when is_favorite then 1 else 0 end) as favorite_titles,
                        sum(case when progress_episode_number is not null then 1 else 0 end) as progress_titles,
                        sum(case when watched then 1 else 0 end) as watched_titles,
                        max(updated_at) as last_state_at
                    from user_title_state
                    group by user_id
                ),
                session_stats as (
                    select
                        user_id,
                        count(*) as active_sessions,
                        max(last_seen_at) as last_session_at
                    from sessions
                    where revoked_at is null
                      and expires_at > ?
                    group by user_id
                )
                select
                    u.id,
                    u.email,
                    u.name,
                    u.picture_url,
                    u.created_at,
                    u.last_login_at,
                    coalesce(ss.touched_titles, 0) as touched_titles,
                    coalesce(ss.favorite_titles, 0) as favorite_titles,
                    coalesce(ss.progress_titles, 0) as progress_titles,
                    coalesce(ss.watched_titles, 0) as watched_titles,
                    ss.last_state_at,
                    coalesce(sess.active_sessions, 0) as active_sessions,
                    sess.last_session_at
                from users u
                left join state_stats ss on ss.user_id = u.id
                left join session_stats sess on sess.user_id = u.id
                order by
                    coalesce(u.last_login_at, u.created_at) desc,
                    u.id desc
                """,
                (now,),
            ).fetchall()
        )
        for user in users:
            for key in (
                "touched_titles",
                "favorite_titles",
                "progress_titles",
                "watched_titles",
                "active_sessions",
            ):
                user[key] = int(user.get(key) or 0)
            user["is_admin"] = is_admin_user(user)

        summary_row = con.execute(
            """
            with state_stats as (
                select
                    user_id,
                    sum(case when is_favorite then 1 else 0 end) as favorite_titles,
                    sum(case when progress_episode_number is not null then 1 else 0 end) as progress_titles,
                    sum(case when watched then 1 else 0 end) as watched_titles
                from user_title_state
                group by user_id
            ),
            session_stats as (
                select user_id, count(*) as active_sessions
                from sessions
                where revoked_at is null
                  and expires_at > ?
                group by user_id
            )
            select
                count(u.id) as registered_users,
                sum(case when u.last_login_at >= ? then 1 else 0 end) as recent_logins,
                sum(case when coalesce(ss.favorite_titles, 0) > 0 then 1 else 0 end) as users_with_favorites,
                sum(case when coalesce(ss.progress_titles, 0) > 0 then 1 else 0 end) as users_with_progress,
                sum(case when coalesce(ss.watched_titles, 0) > 0 then 1 else 0 end) as users_with_watched,
                coalesce(sum(ss.favorite_titles), 0) as total_favorites,
                coalesce(sum(ss.progress_titles), 0) as total_progress_titles,
                coalesce(sum(ss.watched_titles), 0) as total_watched_titles,
                coalesce(sum(sess.active_sessions), 0) as active_sessions
            from users u
            left join state_stats ss on ss.user_id = u.id
            left join session_stats sess on sess.user_id = u.id
            """,
            (now, recent_cutoff),
        ).fetchone()
        summary = dict(summary_row)
        for key, value in list(summary.items()):
            summary[key] = int(value or 0)

        top_titles = rows_to_dicts(
            con.execute(
                """
                select
                    a.id as anime_id,
                    a.title,
                    a.source,
                    count(distinct uts.user_id) as users,
                    sum(case when uts.is_favorite then 1 else 0 end) as favorites,
                    sum(case when uts.progress_episode_number is not null then 1 else 0 end) as in_progress,
                    sum(case when uts.watched then 1 else 0 end) as watched
                from user_title_state uts
                join anime a on a.id = uts.anime_id
                group by a.id, a.title, a.source
                having favorites > 0 or in_progress > 0 or watched > 0
                order by favorites desc, watched desc, in_progress desc, users desc, a.title
                limit 12
                """
            ).fetchall()
        )
        for title in top_titles:
            for key in ("anime_id", "users", "favorites", "in_progress", "watched"):
                title[key] = int(title.get(key) or 0)

        return {
            "summary": summary,
            "users": users,
            "top_titles": top_titles,
            "generated_at": now,
        }
    finally:
        con.close()


def configured_sync_token():
    return os.environ.get("ANIME_SYNC_TOKEN", "").strip()


def configured_animego_push_token():
    return os.environ.get("ANIMEGO_PUSH_TOKEN", "").strip()


def configured_content_sync_sources():
    raw = os.environ.get("ANIME_CONTENT_SYNC_SOURCES", "").strip()
    if not raw:
        return list(CONTENT_SYNC_SOURCES)
    requested = []
    for value in raw.replace(",", " ").split():
        source = value.strip().lower()
        if source and source not in requested:
            requested.append(source)
    invalid = [source for source in requested if source not in CONTENT_SYNC_SOURCES]
    if invalid:
        raise ValueError(f"unsupported content sync source(s): {', '.join(invalid)}")
    if not requested:
        raise ValueError("ANIME_CONTENT_SYNC_SOURCES must enable at least one source")
    return requested


def run_content_sync(db_path, mode="daily", trigger="internal-api"):
    if mode not in SYNC_MODES:
        raise ValueError(f"unsupported sync mode: {mode}")

    import sync_videos

    started = time.perf_counter()
    argv = ["--db", str(db_path), "--mode", mode, "--trigger", trigger]
    for source in configured_content_sync_sources():
        argv.extend(("--source", source))
    args = sync_videos.parse_args(argv)
    try:
        stats = sync_videos.run_sync(args)
    except sync_videos.SyncFailedError as exc:
        stats = exc.stats
    except sync_videos.OperationLockError as exc:
        raise ContentSyncBusyError("content sync is already running") from exc
    except Exception as exc:
        duration_ms = max(0, int((time.perf_counter() - started) * 1000))
        event = {
            "event": "content_sync_error",
            "mode": mode,
            "trigger": trigger,
            "duration_ms": duration_ms,
            "error": str(exc),
            "timestamp": now_iso(),
        }
        server_logger().exception(json.dumps(event, ensure_ascii=False, sort_keys=True))
        raise
    duration_ms = max(0, int((time.perf_counter() - started) * 1000))
    failed = any(
        int(source_stats.get("failed") or 0) > 0
        for source_stats in stats.values()
        if isinstance(source_stats, dict)
    )
    event = {
        "event": "content_sync",
        "status": "partial" if failed else "success",
        "mode": mode,
        "trigger": trigger,
        "duration_ms": duration_ms,
        "stats": stats,
        "timestamp": now_iso(),
    }
    invalidate_catalog_cache(db_path)
    if failed:
        server_logger().warning(json.dumps(event, ensure_ascii=False, sort_keys=True))
        raise ContentSyncPartialError(event)
    server_logger().info(json.dumps(event, ensure_ascii=False, sort_keys=True))
    return event


def run_pushed_animego_sync(db_path, bundle, trigger="trusted-animego-worker"):
    import sync_videos

    started = time.perf_counter()
    try:
        stats = sync_videos.apply_animego_bundle(db_path, bundle, trigger=trigger)
    except sync_videos.SyncFailedError as exc:
        stats = exc.stats
        status = "partial"
    except sync_videos.OperationLockError as exc:
        raise ContentSyncBusyError("content sync is already running") from exc
    except Exception as exc:
        duration_ms = max(0, int((time.perf_counter() - started) * 1000))
        event = {
            "event": "content_sync_push_error",
            "source": "animego",
            "trigger": trigger,
            "duration_ms": duration_ms,
            "error": str(exc),
            "timestamp": now_iso(),
        }
        server_logger().exception(json.dumps(event, ensure_ascii=False, sort_keys=True))
        raise
    else:
        status = "success"

    apply_duration_ms = max(0, int((time.perf_counter() - started) * 1000))
    end_to_end_duration_ms = apply_duration_ms
    try:
        collection_started_at = dt.datetime.fromisoformat(
            str(bundle["collection_started_at"]).replace("Z", "+00:00")
        )
        end_to_end_duration_ms = max(
            apply_duration_ms,
            int((dt.datetime.now(dt.timezone.utc) - collection_started_at).total_seconds() * 1000),
        )
    except (KeyError, TypeError, ValueError):
        pass
    event = {
        "event": "content_sync_push",
        "source": "animego",
        "status": status,
        "mode": str(bundle.get("mode") or "daily"),
        "trigger": trigger,
        "duration_ms": end_to_end_duration_ms,
        "apply_duration_ms": apply_duration_ms,
        "collection_duration_ms": int((bundle.get("collector") or {}).get("duration_ms") or 0),
        "stats": stats,
        "timestamp": now_iso(),
    }
    invalidate_catalog_cache(db_path)
    if status != "success":
        server_logger().warning(json.dumps(event, ensure_ascii=False, sort_keys=True))
        raise ContentSyncPartialError(event)
    server_logger().info(json.dumps(event, ensure_ascii=False, sort_keys=True))
    return event


def env_flag(name):
    return os.environ.get(name, "").strip().lower() in TRUTHY_VALUES


def sync_schedule_time():
    hour = int(os.environ.get("ANIME_DAILY_SYNC_UTC_HOUR", "2"))
    minute = int(os.environ.get("ANIME_DAILY_SYNC_UTC_MINUTE", "0"))
    if not 0 <= hour <= 23:
        raise ValueError("ANIME_DAILY_SYNC_UTC_HOUR must be between 0 and 23")
    if not 0 <= minute <= 59:
        raise ValueError("ANIME_DAILY_SYNC_UTC_MINUTE must be between 0 and 59")
    return hour, minute


def next_daily_sync_run(now=None, hour=2, minute=0):
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    else:
        now = now.astimezone(dt.timezone.utc)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return target


def previous_daily_sync_run(now=None, hour=2, minute=0):
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    else:
        now = now.astimezone(dt.timezone.utc)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target > now:
        target -= dt.timedelta(days=1)
    return target


def content_sync_is_due(db_path, mode, sources, scheduled_at):
    if mode not in SYNC_MODES:
        raise ValueError(f"unsupported sync mode: {mode}")
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=dt.timezone.utc)
    else:
        scheduled_at = scheduled_at.astimezone(dt.timezone.utc)
    try:
        con = sqlite3.connect(db_path, timeout=2)
        try:
            table_exists = con.execute(
                "select 1 from sqlite_master where type = 'table' and name = 'video_sync_state'"
            ).fetchone()
            if not table_exists:
                return True
            keys = [f"{source}:{mode}:last_success" for source in sources]
            if not keys:
                return False
            placeholders = ",".join("?" for _ in keys)
            state = dict(
                con.execute(
                    f"select key, value from video_sync_state where key in ({placeholders})",
                    keys,
                ).fetchall()
            )
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return True

    for key in keys:
        value = state.get(key)
        try:
            succeeded_at = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return True
        if succeeded_at.tzinfo is None:
            succeeded_at = succeeded_at.replace(tzinfo=dt.timezone.utc)
        else:
            succeeded_at = succeeded_at.astimezone(dt.timezone.utc)
        if succeeded_at < scheduled_at:
            return True
    return False


def sleep_until(target):
    while True:
        delay = (target - dt.datetime.now(dt.timezone.utc)).total_seconds()
        if delay <= 0:
            return
        time.sleep(min(delay, 60))


def daily_sync_scheduler_loop(db_path):
    try:
        hour, minute = sync_schedule_time()
    except Exception:
        server_logger().exception("daily sync scheduler has invalid schedule")
        return

    while True:
        mode = os.environ.get("ANIME_SYNC_MODE", "daily").strip() or "daily"
        sources = configured_content_sync_sources()
        now = dt.datetime.now(dt.timezone.utc)
        scheduled_at = previous_daily_sync_run(now, hour=hour, minute=minute)
        if not content_sync_is_due(db_path, mode, sources, scheduled_at):
            next_run = next_daily_sync_run(now, hour=hour, minute=minute)
            server_logger().info(
                json.dumps(
                    {
                        "event": "content_sync_scheduler_scheduled",
                        "scheduled_at": next_run.isoformat(timespec="seconds"),
                        "hour_utc": hour,
                        "minute_utc": minute,
                        "timestamp": now_iso(),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            sleep_until(next_run)
            continue

        server_logger().info(
            json.dumps(
                {
                    "event": "content_sync_scheduler_start",
                    "mode": mode,
                    "scheduled_at": scheduled_at.isoformat(timespec="seconds"),
                    "timestamp": now_iso(),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        try:
            run_content_sync(db_path, mode=mode, trigger="internal-daily-scheduler")
        except ContentSyncBusyError as exc:
            retry_seconds = CONTENT_SYNC_BUSY_RETRY_SECONDS
            retry_reason = "busy"
            server_logger().warning(f"content sync scheduler is busy: {exc}")
        except Exception:
            retry_seconds = CONTENT_SYNC_ERROR_RETRY_SECONDS
            retry_reason = "failed"
            server_logger().exception("content sync scheduler failed")
        else:
            continue

        next_attempt = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=retry_seconds)
        server_logger().info(
            json.dumps(
                {
                    "event": "content_sync_scheduler_retry",
                    "scheduled_at": scheduled_at.isoformat(timespec="seconds"),
                    "next_attempt_at": next_attempt.isoformat(timespec="seconds"),
                    "retry_reason": retry_reason,
                    "timestamp": now_iso(),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        sleep_until(next_attempt)


def recover_interrupted_content_sync_runs(db_path):
    try:
        with DatabaseOperationLock(db_path, operation="recover interrupted content sync runs"):
            con = connect(db_path)
            try:
                content_updates.ensure_schema(con)
                recovered = content_updates.fail_running_runs(con)
                con.commit()
            finally:
                con.close()
    except OperationLockError:
        server_logger().warning("skipped interrupted sync recovery because the database lock is busy")
        return 0
    if recovered:
        server_logger().warning(f"marked {recovered} interrupted content sync run(s) as failed")
    return recovered


def start_daily_sync_scheduler(db_path):
    if not env_flag("ANIME_INTERNAL_DAILY_SYNC"):
        return None
    thread = threading.Thread(
        target=daily_sync_scheduler_loop,
        args=(str(db_path),),
        name="anime-daily-sync-scheduler",
        daemon=True,
    )
    thread.start()
    return thread


def safe_next_path(value):
    raw_value = str(value or "/")
    parsed = urlparse(raw_value)
    if parsed.scheme or parsed.netloc:
        return "/"
    path = parsed.path or "/"
    decoded_path = unquote(path)
    if (
        not path.startswith("/")
        or "\\" in decoded_path
        or any(ord(character) < 32 for character in decoded_path)
    ):
        return "/"
    query = f"?{parsed.query}" if parsed.query else ""
    fragment = f"#{parsed.fragment}" if parsed.fragment else ""
    return f"{path}{query}{fragment}"


def inline_script_json(value):
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def google_auth_state_secret():
    configured = os.environ.get(GOOGLE_AUTH_STATE_SECRET_ENV, "")
    if not configured:
        return GOOGLE_AUTH_STATE_FALLBACK_SECRET
    secret_bytes = configured.encode("utf-8")
    if len(secret_bytes) < 32:
        raise AuthConfigError(f"{GOOGLE_AUTH_STATE_SECRET_ENV} must be at least 32 bytes")
    return hashlib.sha256(b"anime-google-auth-state\0" + secret_bytes).digest()


def base64url_encode(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def base64url_decode(value):
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}")


def sign_google_auth_state(next_path):
    payload = {
        "iat": int(time.time()),
        "next": safe_next_path(next_path),
        "nonce": secrets.token_urlsafe(16),
    }
    payload_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(google_auth_state_secret(), payload_bytes, hashlib.sha256).digest()
    return f"{base64url_encode(payload_bytes)}.{base64url_encode(signature)}"


def verify_google_auth_state(value):
    try:
        payload_part, signature_part = (value or "").split(".", 1)
        payload_bytes = base64url_decode(payload_part)
        signature = base64url_decode(signature_part)
    except (ValueError, TypeError, binascii.Error):
        raise AuthError(GOOGLE_AUTH_STATE_ERROR) from None

    expected = hmac.new(google_auth_state_secret(), payload_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise AuthError(GOOGLE_AUTH_STATE_ERROR)

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
        issued_at = int(payload.get("iat") or 0)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise AuthError(GOOGLE_AUTH_STATE_ERROR) from None

    now = int(time.time())
    if issued_at < now - GOOGLE_AUTH_STATE_TTL_SECONDS or issued_at > now + 60:
        raise AuthError(GOOGLE_AUTH_STATE_ERROR)
    return safe_next_path(payload.get("next") or "/")


def create_login_handoff(session_token, next_path, db_path=None):
    code = secrets.token_urlsafe(32)
    created_at = now_iso()
    expires_at = (
        dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=LOGIN_HANDOFF_TTL_SECONDS)
    ).isoformat(timespec="seconds")
    con = connect(db_path)
    try:
        con.execute("begin immediate")
        session = con.execute(
            """
            select user_id
            from sessions
            where token_hash = ?
              and revoked_at is null
              and expires_at > ?
            """,
            (session_token_hash(session_token), created_at),
        ).fetchone()
        if not session:
            raise AuthError("Сессия входа истекла. Попробуйте войти еще раз.")
        con.execute("delete from login_handoffs where expires_at <= ?", (created_at,))
        con.execute(
            """
            insert into login_handoffs(code_hash, user_id, next_path, created_at, expires_at)
            values (?, ?, ?, ?, ?)
            """,
            (
                session_token_hash(code),
                session["user_id"],
                safe_next_path(next_path),
                created_at,
                expires_at,
            ),
        )
        # The provisional session never leaves this process and is invalidated
        # as soon as the one-time handoff exists. The browser gets a fresh
        # session only after atomically consuming the handoff.
        con.execute(
            "delete from sessions where token_hash = ?",
            (session_token_hash(session_token),),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return code


def consume_login_handoff(code, db_path=None):
    if not code:
        raise AuthError("Сессия входа истекла. Попробуйте войти еще раз.")
    code_hash = session_token_hash(code)
    timestamp = now_iso()
    con = connect(db_path)
    try:
        # Missing/expired random codes stay a read-only path. In particular,
        # scanner traffic cannot acquire SQLite's global writer reservation.
        row = con.execute(
            """
            select user_id, next_path, expires_at
            from login_handoffs
            where code_hash = ?
            """,
            (code_hash,),
        ).fetchone()
        if not row or row["expires_at"] <= timestamp:
            raise AuthError("Сессия входа истекла. Попробуйте войти еще раз.")

        con.execute("begin immediate")
        row = con.execute(
            """
            select h.user_id, h.next_path, h.expires_at
            from login_handoffs h
            join users u on u.id = h.user_id
            where h.code_hash = ? and h.expires_at > ?
            """,
            (code_hash, timestamp),
        ).fetchone()
        if not row:
            con.rollback()
            raise AuthError("Сессия входа истекла. Попробуйте войти еще раз.")
        con.execute(
            "delete from login_handoffs where code_hash = ?",
            (code_hash,),
        )
        token, _ = create_session(con, row["user_id"])
        con.commit()
    except AuthError:
        if con.in_transaction:
            con.rollback()
        raise
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return token, row["next_path"]


def accepts_content_encoding(header, encoding):
    qualities = {}
    for part in str(header or "").split(","):
        tokens = [token.strip() for token in part.split(";") if token.strip()]
        if not tokens:
            continue
        name = tokens[0].lower()
        quality = 1.0
        for parameter in tokens[1:]:
            key, separator, value = parameter.partition("=")
            if separator and key.strip().lower() == "q":
                try:
                    quality = max(0.0, min(1.0, float(value.strip())))
                except ValueError:
                    quality = 0.0
        qualities[name] = quality
    key = encoding.lower()
    if key in qualities:
        return qualities[key] > 0
    return qualities.get("*", 0.0) > 0


class AnimeHandler(BaseHTTPRequestHandler):
    server_version = "AnimeLocal/0.1"

    def send_response(self, code, message=None):
        self._last_status = code
        super().send_response(code, message)

    def log_message(self, fmt, *args):
        parsed = urlparse(getattr(self, "path", "") or "")
        server_logger().info(
            "remote=%s method=%s path=%s status=%s message=%s",
            self.client_address[0] if self.client_address else "-",
            getattr(self, "command", "-"),
            parsed.path or "-",
            getattr(self, "_last_status", "-"),
            fmt % args,
        )

    def handle_request(self, callback):
        started_at = time.perf_counter()
        caught = None
        try:
            callback()
        except (BrokenPipeError, ConnectionResetError) as exc:
            caught = exc
            server_logger().debug(
                "client disconnected while writing response remote=%s method=%s path=%s",
                self.client_address[0] if self.client_address else "-",
                getattr(self, "command", "-"),
                urlparse(getattr(self, "path", "") or "").path or "-",
            )
        except Exception as exc:
            caught = exc
            self.send_unexpected_error(exc)
        finally:
            self.log_request_performance(started_at, caught)

    def log_request_performance(self, started_at, exc=None):
        parsed = urlparse(getattr(self, "path", "") or "")
        event = {
            "received_at": now_iso(),
            "event": "server_request",
            "method": getattr(self, "command", "-"),
            "path": parsed.path or "-",
            "status": getattr(self, "_last_status", None),
            "duration_ms": round((time.perf_counter() - started_at) * 1000, 1),
            "response_bytes": getattr(self, "_last_response_bytes", None),
        }
        if hasattr(self, "_current_user"):
            event["authenticated"] = bool(self._current_user)
            if self._current_user:
                event["user_id"] = self._current_user["id"]
        if exc is not None:
            event["error_type"] = type(exc).__name__
        performance_logger().info(json.dumps(event, ensure_ascii=False, sort_keys=True))

    def send_unexpected_error(self, exc):
        parsed = urlparse(getattr(self, "path", "") or "")
        server_logger().exception(
            "unhandled request error remote=%s method=%s path=%s",
            self.client_address[0] if self.client_address else "-",
            getattr(self, "command", "-"),
            parsed.path or "-",
        )
        try:
            self.send_json({"error": "internal server error"}, 500)
        except (BrokenPipeError, ConnectionResetError, OSError):
            server_logger().warning(
                "failed to write error response remote=%s method=%s path=%s",
                self.client_address[0] if self.client_address else "-",
                getattr(self, "command", "-"),
                parsed.path or "-",
            )

    def send_security_headers(self, script_nonce=None):
        self.send_header("Content-Security-Policy", content_security_policy(script_nonce))
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")

    def send_json(self, payload, status=200, headers=None):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        encoding_headers = []
        if len(body) > 1024 and accepts_content_encoding(self.headers.get("Accept-Encoding"), "gzip"):
            compressed = gzip.compress(body, compresslevel=5)
            if len(compressed) < len(body):
                body = compressed
                encoding_headers = [("Content-Encoding", "gzip"), ("Vary", "Accept-Encoding")]
        self._last_response_bytes = len(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_security_headers()
        for name, value in encoding_headers:
            self.send_header(name, value)
        for name, value in (headers or []):
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html_body, status=200, headers=None, script_nonce=None):
        body = html_body.encode("utf-8")
        self._last_response_bytes = len(body)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_security_headers(script_nonce)
        for name, value in (headers or []):
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_redirect(self, location, status=302, headers=None):
        self._last_response_bytes = 0
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_security_headers()
        for name, value in (headers or []):
            self.send_header(name, value)
        self.end_headers()

    def read_body_text(self, max_bytes=MAX_JSON_BODY_BYTES):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if length < 0:
            raise ValueError("invalid content length")
        if length > max_bytes:
            self.close_connection = True
            self.rfile.read(0)
            raise ClientErrorPayloadTooLarge()
        if not length:
            return ""
        return self.rfile.read(length).decode("utf-8")

    def read_json_body(self):
        raw = self.read_body_text(MAX_JSON_BODY_BYTES)
        return json.loads(raw or "{}")

    def read_limited_json_body(self, max_bytes):
        raw = self.read_body_text(max_bytes)
        return json.loads(raw or "{}")

    def build_client_error_event(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        message = payload.get("message")
        if not message:
            raise ValueError("message is required")

        user = self.current_user()
        event = {
            "received_at": now_iso(),
            "remote": self.client_address[0] if self.client_address else None,
            "authenticated": bool(user),
            "type": sanitize_client_error_value(payload.get("type") or "error"),
            "message": sanitize_client_error_value(message),
        }
        if user:
            event["user_id"] = user["id"]

        for key in (
            "timestamp",
            "url",
            "path",
            "source",
            "lineno",
            "colno",
            "stack",
            "userAgent",
            "context",
        ):
            if key in payload:
                event[key] = sanitize_client_error_value(payload[key])
        return event

    def handle_client_error_post(self):
        try:
            payload = self.read_limited_json_body(MAX_CLIENT_ERROR_BYTES)
            event = self.build_client_error_event(payload)
        except ClientErrorPayloadTooLarge:
            self.send_json({"error": "payload too large"}, 413)
            return
        except json.JSONDecodeError:
            self.send_json({"error": "invalid json"}, 400)
            return
        except ValueError as exc:
            self.send_json({"error": str(exc)}, 400)
            return

        client_error_logger().info(json.dumps(event, ensure_ascii=False, sort_keys=True))
        self.send_json({"ok": True}, status=202)

    def build_performance_event(self, payload, user):
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        event_name = payload.get("event") or payload.get("type")
        if not event_name:
            raise ValueError("event is required")

        event = {
            "received_at": now_iso(),
            "authenticated": True,
            "event": sanitize_client_error_value(event_name),
            "remote": self.client_address[0] if self.client_address else None,
            "user_id": user["id"],
        }
        for key in (
            "timestamp",
            "path",
            "url",
            "source",
            "result",
            "duration_ms",
            "navigation",
            "resources",
            "api_requests",
            "checkpoints",
            "viewport",
            "connection",
            "catalog",
            "context",
            "userAgent",
        ):
            if key in payload:
                event[key] = sanitize_client_error_value(payload[key])
        return event

    def handle_performance_post(self, user):
        try:
            payload = self.read_limited_json_body(MAX_PERFORMANCE_EVENT_BYTES)
            event = self.build_performance_event(payload, user)
        except ClientErrorPayloadTooLarge:
            self.send_json({"error": "payload too large"}, 413)
            return
        except json.JSONDecodeError:
            self.send_json({"error": "invalid json"}, 400)
            return
        except ValueError as exc:
            self.send_json({"error": str(exc)}, 400)
            return

        performance_logger().info(json.dumps(event, ensure_ascii=False, sort_keys=True))
        self.send_json({"ok": True}, status=202)

    def read_google_auth_body(self):
        raw = self.read_body_text(MAX_JSON_BODY_BYTES)
        if not raw:
            return {}, False
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type == "application/x-www-form-urlencoded":
            params = parse_qs(raw, keep_blank_values=True)
            return {key: values[0] for key, values in params.items()}, True
        return json.loads(raw or "{}"), False

    def send_static(self, path):
        safe_path = path.lstrip("/") or "index.html"
        if safe_path == "":
            safe_path = "index.html"
        static_root = STATIC_DIR.resolve()
        target = (static_root / safe_path).resolve()
        if not target.is_relative_to(static_root) or not target.is_file():
            self.send_json({"error": "not found"}, 404)
            return
        content = target.read_bytes()
        self._last_response_bytes = len(content)
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix == ".js":
            ctype = "text/javascript"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_security_headers()
        if safe_path == "login.html":
            self.send_header("Cross-Origin-Opener-Policy", "same-origin-allow-popups")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def session_tokens(self):
        if hasattr(self, "_session_tokens"):
            return self._session_tokens

        # Google GIS stores raw JSON in its first-party g_state cookie. Python's
        # SimpleCookie rejects the entire header when that value appears before
        # our session cookie, which is a common Firefox cookie order. Parse only
        # the cookie we own and retain a few candidates so stale duplicates
        # cannot hide a newer valid session.
        values = []
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, separator, value = part.strip().partition("=")
            value = value.strip()
            if (
                not separator
                or name != SESSION_COOKIE_NAME
                or not value
                or len(value) > MAX_SESSION_TOKEN_CHARS
                or value in values
            ):
                continue
            values.append(value)
            if len(values) >= MAX_SESSION_COOKIE_CANDIDATES:
                break
        self._session_tokens = tuple(values)
        return self._session_tokens

    def session_token(self):
        selected = getattr(self, "_current_session_token", None)
        if selected:
            return selected
        return next(iter(self.session_tokens()), None)

    def current_user(self):
        if hasattr(self, "_current_user"):
            return self._current_user
        self._current_user = None
        self._current_session_token = None
        for token in self.session_tokens():
            user = get_session_user(token, self.server.db_path)
            if user:
                self._current_user = user
                self._current_session_token = token
                break
        return self._current_user

    def require_user(self):
        user = self.current_user()
        if user:
            return user
        self.send_json({"error": "authentication required"}, 401)
        return None

    def require_admin(self):
        user = self.require_user()
        if not user:
            return None
        if user.get("is_admin"):
            return user
        self.send_json({"error": "not found"}, 404)
        return None

    def sync_request_token(self):
        auth = self.headers.get("Authorization", "").strip()
        prefix = "bearer "
        if auth.lower().startswith(prefix):
            return auth[len(prefix):].strip()
        return self.headers.get("X-Anime-Sync-Token", "").strip()

    def require_sync_token(self):
        expected = configured_sync_token()
        if not expected:
            self.send_json({"error": "sync token is not configured"}, 503)
            return False
        received = self.sync_request_token()
        if not received or not hmac.compare_digest(received, expected):
            self.send_json({"error": "authentication required"}, 401)
            return False
        return True

    def require_animego_push_token(self):
        expected = configured_animego_push_token()
        if not expected:
            self.send_json({"error": "AnimeGO push token is not configured"}, 503)
            return False
        received = self.sync_request_token()
        if not received or not hmac.compare_digest(received, expected):
            self.send_json({"error": "authentication required"}, 401)
            return False
        return True

    def current_user_is_admin(self):
        user = self.current_user()
        return bool(user and user.get("is_admin"))

    def session_cookie_header(self, token):
        parts = [
            f"{SESSION_COOKIE_NAME}={token}",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            f"Max-Age={SESSION_TTL_SECONDS}",
        ]
        if session_cookie_secure():
            parts.append("Secure")
        return "; ".join(parts)

    def clear_session_cookie_header(self):
        parts = [
            f"{SESSION_COOKIE_NAME}=",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            "Max-Age=0",
        ]
        if session_cookie_secure():
            parts.append("Secure")
        return "; ".join(parts)

    def redirect_to_login(self):
        next_path = quote(safe_next_path(self.path), safe="")
        self.send_redirect(f"/login?next={next_path}")

    def redirect_to_login_auth_error(self, message, next_path=None):
        next_path = safe_next_path(next_path or "/")
        params = {"auth_error": message}
        if next_path != "/":
            params["next"] = next_path
        self.send_redirect(f"/login?{urlencode(params)}")

    def is_google_redirect_request(self):
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
        return content_type.strip().lower() == "application/x-www-form-urlencoded"

    def send_google_auth_error(self, message, status, is_redirect_flow, next_path=None):
        if is_redirect_flow:
            self.redirect_to_login_auth_error(message, next_path)
            return
        self.send_json({"error": message}, status)

    def send_login_complete_page(self, token, next_path):
        next_path = safe_next_path(next_path)
        script_nonce = secrets.token_urlsafe(18)
        next_js = inline_script_json(next_path)
        next_href = html.escape(next_path, quote=True)
        recovery_path = f"/login?{urlencode({'next': next_path, 'auth_complete': '1'})}"
        recovery_js = inline_script_json(recovery_path)
        self.send_html(
            f"""<!doctype html>
<html lang=\"ru\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Вход выполнен - Anime Catalog</title>
  <script src=\"/static/client_errors.js\"></script>
</head>
<body>
  <p id=\"login-complete-state\">Вход выполнен. Открываю приложение...</p>
  <p><a href=\"{next_href}\">Открыть приложение</a></p>
  <script nonce="{script_nonce}">
    const nextPath = {next_js};
    const state = document.getElementById("login-complete-state");
    const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
    const sessionDeadline = Date.now() + 12_000;

    async function waitForSession() {{
      let attempts = 0;
      let lastStatus = null;
      for (let attempt = 0; attempt < 120 && Date.now() < sessionDeadline; attempt += 1) {{
        attempts = attempt + 1;
        try {{
          const response = await fetch("/api/me", {{
            cache: "no-store",
            credentials: "same-origin",
          }});
          lastStatus = response.status;
          if (response.ok) {{
            window.location.replace(nextPath);
            return;
          }}
        }} catch (error) {{
          lastStatus = null;
          // The next retry handles transient navigation/cookie timing.
        }}
        await delay(100);
      }}
      state.textContent = "Вход выполнен. Открываю приложение...";
      const report = window.reportClientError?.(
        new Error("Login completion session recovery timed out"),
        {{
          type: "login.session_completion_timeout",
          action: "wait for login session",
          attempts,
          recoveryWindowMs: 12_000,
          lastStatus,
          online: navigator.onLine,
          visibilityState: document.visibilityState,
        }},
      );
      if (report) await Promise.race([report, delay(500)]);
      window.location.replace({recovery_js});
    }}

    waitForSession();
  </script>
</body>
</html>
""",
            headers=[("Set-Cookie", self.session_cookie_header(token))],
            script_nonce=script_nonce,
        )

    def do_GET(self):
        self.handle_request(self.handle_GET)

    def handle_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/favicon.ico":
            self.send_static("favicon.svg")
            return

        if path.startswith("/static/"):
            if path.startswith("/static/admin") and not self.current_user_is_admin():
                self.send_json({"error": "not found"}, 404)
                return
            self.send_static(path.removeprefix("/static/"))
            return

        if path == "/api/health":
            if database_is_ready(self.server.db_path):
                self.send_json({"ok": True})
            else:
                self.send_json({"ok": False, "error": "database unavailable"}, 503)
            return

        if path == "/api/internal/animego-sync-manifest":
            if not self.require_animego_push_token():
                return
            import sync_videos

            self.send_json(sync_videos.animego_sync_manifest(self.server.db_path))
            return

        if path == "/api/auth/config":
            client_id = google_client_id()
            next_path = safe_next_path(parse_qs(parsed.query).get("next", ["/"])[0])
            try:
                state = sign_google_auth_state(next_path) if client_id else ""
            except AuthConfigError as exc:
                self.send_json(
                    {
                        "configured": False,
                        "client_id": client_id,
                        "state": "",
                        "error": f"deployment configuration error: {exc}",
                    },
                    503,
                )
                return
            self.send_json(
                {
                    "configured": bool(client_id),
                    "client_id": client_id,
                    "state": state,
                }
            )
            return

        if path == "/login":
            if self.current_user():
                next_path = safe_next_path(parse_qs(parsed.query).get("next", ["/"])[0])
                self.send_redirect(next_path)
                return
            self.send_static("login.html")
            return

        if path == "/api/auth/complete":
            try:
                token, next_path = consume_login_handoff(
                    parse_qs(parsed.query).get("code", [""])[0],
                    self.server.db_path,
                )
            except AuthError as exc:
                self.redirect_to_login_auth_error(str(exc) or "Не удалось войти через Google")
                return
            self.send_login_complete_page(token, next_path)
            return

        if path == "/api/me":
            user = self.require_user()
            if user:
                self.send_json({"user": user})
            return

        if path == "/admin" or path == "/admin/":
            user = self.current_user()
            if not user:
                self.redirect_to_login()
                return
            if not user.get("is_admin"):
                self.send_json({"error": "not found"}, 404)
                return
            self.send_static("admin.html")
            return

        user = self.current_user()
        if path.startswith("/api/") and not user:
            self.send_json({"error": "authentication required"}, 401)
            return

        if path == "/api/app-config":
            self.send_json({"player_hosts": list(PLAYER_HOSTS)})
            return

        if path == "/api/admin/users":
            if self.require_admin():
                self.send_json(admin_users_payload(self.server.db_path))
            return

        if path == "/api/anime":
            query = parse_qs(parsed.query).get("q", [""])[0].strip()
            items = get_anime_list(self.server.db_path, query or None, user["id"])
            self.send_json({"items": catalog_api_items(items)})
            return

        if path == "/api/anime/search-fields":
            self.send_json({"items": get_anime_search_fields(self.server.db_path, user["id"])})
            return

        if path == "/api/recommendations":
            query = parse_qs(parsed.query)
            raw_limit = query.get("limit", [str(DEFAULT_RECOMMENDATION_LIMIT)])[0]
            filters = {
                key: query[key][0]
                for key in ("genre", "year", "year_from", "year_to", "kind", "status", "source", "video")
                if query.get(key)
            }
            self.send_json(get_recommendations(self.server.db_path, raw_limit, user["id"], filters))
            return

        if path == "/api/content-updates":
            query = parse_qs(parsed.query)
            raw_days = query.get("days", [str(DEFAULT_CONTENT_UPDATE_DAYS)])[0]
            raw_limit = query.get("limit", [str(DEFAULT_CONTENT_UPDATE_LIMIT)])[0]
            raw_event_type = query.get("event_type", ["all"])[0]
            raw_offset = query.get("offset", ["0"])[0]
            try:
                payload = get_content_updates(
                    self.server.db_path,
                    raw_days,
                    raw_limit,
                    user["id"],
                    event_type=raw_event_type,
                    offset=raw_offset,
                )
            except ValueError as error:
                self.send_json({"error": str(error)}, 400)
                return
            self.send_json(payload)
            return

        if path == "/api/continue-watching":
            self.send_json(get_continue_watching(self.server.db_path, user["id"]))
            return

        if path.startswith("/api/anime/"):
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "anime":
                detail = get_anime_detail(unquote(parts[2]), self.server.db_path, user["id"])
                if detail:
                    self.send_json(detail)
                else:
                    self.send_json({"error": "not found"}, 404)
                return

        if path == "/" or re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9-]*", path):
            if not user:
                self.redirect_to_login()
                return
            self.send_static("index.html")
            return

        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        self.handle_request(self.handle_POST)

    def handle_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/internal/daily-sync":
            if not self.require_sync_token():
                return
            mode = parse_qs(parsed.query).get("mode", [os.environ.get("ANIME_SYNC_MODE", "daily")])[0]
            try:
                result = run_content_sync(self.server.db_path, mode=mode, trigger="railway-cron")
            except ContentSyncBusyError as exc:
                self.send_json({"ok": False, "status": "busy", "error": str(exc)}, 423)
                return
            except ContentSyncPartialError as exc:
                self.send_json({"ok": False, **exc.event}, 502)
                return
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
                return
            except Exception as exc:
                server_logger().exception("content sync failed")
                self.send_json({"error": str(exc) or "sync failed"}, 500)
                return
            self.send_json({"ok": True, **result})
            return

        if path == "/api/internal/animego-push-sync":
            if not self.require_animego_push_token():
                return
            try:
                bundle = self.read_limited_json_body(MAX_ANIMEGO_PUSH_BODY_BYTES)
                result = run_pushed_animego_sync(self.server.db_path, bundle)
            except ClientErrorPayloadTooLarge:
                self.send_json({"error": "payload too large"}, 413)
                return
            except json.JSONDecodeError:
                self.send_json({"error": "invalid json"}, 400)
                return
            except ContentSyncBusyError as exc:
                self.send_json({"ok": False, "status": "busy", "error": str(exc)}, 423)
                return
            except ContentSyncPartialError as exc:
                self.send_json({"ok": False, **exc.event}, 502)
                return
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
                return
            except Exception:
                server_logger().exception("trusted AnimeGO push sync failed")
                self.send_json({"error": "sync failed"}, 500)
                return
            self.send_json({"ok": True, **result})
            return

        if path == "/api/client-errors":
            self.handle_client_error_post()
            return

        if path == "/api/performance":
            user = self.require_user()
            if user:
                self.handle_performance_post(user)
            return

        if path == "/api/watch-events":
            user = self.require_user()
            if not user:
                return
            try:
                payload = self.read_json_body()
                result = record_watch_event(payload, self.server.db_path, user["id"])
            except ClientErrorPayloadTooLarge:
                self.send_json({"error": "payload too large"}, 413)
                return
            except json.JSONDecodeError:
                self.send_json({"error": "invalid json"}, 400)
                return
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
                return
            self.send_json(result)
            return

        if path == "/api/auth/google":
            payload = {}
            is_redirect_flow = self.is_google_redirect_request()
            next_path = "/"
            try:
                payload, is_redirect_flow = self.read_google_auth_body()
                if is_redirect_flow:
                    next_path = verify_google_auth_state(payload.get("state"))
                elif payload.get("state"):
                    next_path = verify_google_auth_state(payload.get("state"))
                else:
                    next_path = safe_next_path(payload.get("next") or "/")
                auth = authenticate_google_credential(payload.get("credential"), self.server.db_path)
            except ClientErrorPayloadTooLarge:
                self.send_google_auth_error("payload too large", 413, is_redirect_flow, next_path)
                return
            except json.JSONDecodeError:
                self.send_google_auth_error("invalid json", 400, is_redirect_flow)
                return
            except ValueError as exc:
                self.send_google_auth_error(str(exc), 400, is_redirect_flow, next_path)
                return
            except AuthConfigError as exc:
                self.send_google_auth_error(
                    f"Ошибка конфигурации деплоймента: {exc}",
                    503,
                    is_redirect_flow,
                    next_path,
                )
                return
            except AuthError as exc:
                self.send_google_auth_error(
                    str(exc) or "Не удалось войти через Google",
                    401,
                    is_redirect_flow,
                    next_path,
                )
                return
            code = create_login_handoff(auth["token"], next_path, self.server.db_path)
            complete_url = f"/api/auth/complete?code={quote(code, safe='')}"
            if is_redirect_flow:
                self.send_redirect(complete_url)
                return
            self.send_json(
                {"user": auth["user"], "complete_url": complete_url},
            )
            return

        if path == "/api/logout":
            for token in self.session_tokens():
                revoke_session(token, self.server.db_path)
            self.send_json(
                {"ok": True},
                headers=[("Set-Cookie", self.clear_session_cookie_header())],
            )
            return

        if path.startswith("/api/"):
            self.send_json({"error": "authentication required"}, 401)
            return

        self.send_json({"error": "not found"}, 404)

    def do_PATCH(self):
        self.handle_request(self.handle_PATCH)

    def handle_PATCH(self):
        parsed = urlparse(self.path)
        path = parsed.path
        user = self.require_user()
        if not user:
            return

        if path.startswith("/api/anime/"):
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "anime" and parts[3] == "navigation":
                try:
                    payload = self.read_json_body()
                    if not isinstance(payload, dict):
                        raise ValueError("navigation payload must be an object")
                    updated = update_title_navigation(
                        unquote(parts[2]),
                        payload.get("episode_id"),
                        self.server.db_path,
                        user["id"],
                    )
                except ClientErrorPayloadTooLarge:
                    self.send_json({"error": "payload too large"}, 413)
                    return
                except json.JSONDecodeError:
                    self.send_json({"error": "invalid json"}, 400)
                    return
                except ValueError as exc:
                    self.send_json({"error": str(exc)}, 400)
                    return
                if updated is None:
                    self.send_json({"error": "not found"}, 404)
                else:
                    self.send_json({"navigation": updated})
                return
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "anime" and parts[3] == "state":
                try:
                    payload = self.read_json_body()
                    updated = update_user_state(unquote(parts[2]), payload, self.server.db_path, user["id"])
                except ClientErrorPayloadTooLarge:
                    self.send_json({"error": "payload too large"}, 413)
                    return
                except json.JSONDecodeError:
                    self.send_json({"error": "invalid json"}, 400)
                    return
                except ValueError as exc:
                    self.send_json({"error": str(exc)}, 400)
                    return
                if updated is None:
                    self.send_json({"error": "not found"}, 404)
                else:
                    self.send_json({"state": updated})
                return

        self.send_json({"error": "not found"}, 404)


def run(port, host, db_path):
    db_path = prepare_database(db_path)
    configure_logging()
    recover_interrupted_content_sync_runs(db_path)
    prewarm_catalog_cache(db_path)
    server = ThreadingHTTPServer((host, port), AnimeHandler)
    server.db_path = str(db_path)
    message = f"Serving http://{host}:{port} using {db_path}"
    print(message)
    server_logger().info(message)
    start_daily_sync_scheduler(db_path)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")
        server_logger().info("Stopped")
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description="Run the local AnimeGO SQLite browser.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    args = parser.parse_args()
    load_env_file(args.env_file)
    run(args.port, args.host, args.db)


if __name__ == "__main__":
    main()
