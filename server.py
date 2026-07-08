#!/usr/bin/env python3
import argparse
import base64
import binascii
import datetime as dt
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
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

import content_updates

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "db" / "animego.sqlite"
STATIC_DIR = ROOT / "static"
DEFAULT_LOG_DIR = ROOT / "data" / "logs"
DEFAULT_RECOMMENDATION_LIMIT = 20
MAX_RECOMMENDATION_LIMIT = 50
MAX_JSON_BODY_BYTES = 64 * 1024
MAX_CLIENT_ERROR_BYTES = 16 * 1024
MAX_PERFORMANCE_EVENT_BYTES = 24 * 1024
MAX_CLIENT_ERROR_TEXT = 2048
MAX_CLIENT_ERROR_COLLECTION_ITEMS = 20
SYNC_MODES = {"hourly", "daily", "full"}
TRUTHY_VALUES = {"1", "true", "yes", "on"}
SYNTHETIC_RATING_PRIOR = 6.8
SYNTHETIC_RATING_MIN_COUNT = 80
SESSION_COOKIE_NAME = "anime_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 30
GOOGLE_AUTH_STATE_TTL_SECONDS = 10 * 60
GOOGLE_AUTH_STATE_SECRET = secrets.token_bytes(32)
GOOGLE_AUTH_STATE_ERROR = "Не удалось подтвердить ответ Google. Попробуйте войти еще раз."
LOGIN_HANDOFF_TTL_SECONDS = 60
LOGIN_HANDOFFS = {}
LOGIN_HANDOFFS_LOCK = threading.RLock()
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
EXTERNAL_RATING_SOURCES = {
    "tal": ("TAL", 0),
    "myanimelist": ("MAL", 1),
    "mal": ("MAL", 1),
    "anilist": ("AniList", 2),
    "shikimori": ("Shikimori", 3),
    "imdb": ("IMDB", 4),
}
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
CATALOG_CACHE = {}
CATALOG_CACHE_LOCK = threading.RLock()
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
            updated_at text not null,
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
        return True

    if not user_title_state_needs_rebuild(con):
        return False

    old_columns = {row[1] for row in con.execute("pragma table_info(user_title_state)").fetchall()}
    con.execute("alter table user_title_state rename to user_title_state_old")
    create_user_title_state_table(con)

    if "user_id" in old_columns:
        con.execute(
            """
            insert or replace into user_title_state (
                user_id,
                anime_id,
                is_favorite,
                progress_episode_number,
                watched,
                updated_at
            )
            select
                user_id,
                anime_id,
                coalesce(is_favorite, 0),
                progress_episode_number,
                coalesce(watched, 0),
                coalesce(updated_at, ?)
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
    return True


def purge_orphaned_user_data(con):
    if not con.execute("select 1 from sqlite_master where type = 'table' and name = 'users'").fetchone():
        return False

    before = con.total_changes
    if con.execute("select 1 from sqlite_master where type = 'table' and name = 'sessions'").fetchone():
        orphaned_sessions = con.execute(
            """
            select 1
            from sessions
            where not exists (
                select 1
                from users u
                where u.id = sessions.user_id
            )
            limit 1
            """
        ).fetchone()
        if orphaned_sessions:
            con.execute(
                """
                delete from sessions
                where not exists (
                    select 1
                    from users u
                    where u.id = sessions.user_id
                )
                """
            )

    if con.execute("select 1 from sqlite_master where type = 'table' and name = 'user_title_state'").fetchone():
        state_columns = {row[1] for row in con.execute("pragma table_info(user_title_state)").fetchall()}
        if "user_id" in state_columns:
            orphaned_state = con.execute(
                """
                select 1
                from user_title_state
                where not exists (
                    select 1
                    from users u
                    where u.id = user_title_state.user_id
                )
                limit 1
                """
            ).fetchone()
            if orphaned_state:
                con.execute(
                    """
                    delete from user_title_state
                    where not exists (
                        select 1
                        from users u
                        where u.id = user_title_state.user_id
                    )
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
    return changed


def resolve_db_path(db_path=None):
    return Path(db_path or os.environ.get("ANIMEGO_DB") or DEFAULT_DB)


def truthy_env(name):
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


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
    return bool(result["applied"])


def db_signature(path):
    stat = path.stat()
    return (stat.st_mtime_ns, stat.st_size)


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


def connect(db_path=None):
    path = resolve_db_path(db_path)
    ensure_base_database(path)
    con = sqlite3.connect(path)
    con.execute("pragma busy_timeout=30000")
    con.row_factory = sqlite3.Row
    changed = ensure_catalog_schema(con)
    changed |= ensure_auth_schema(con)
    changed |= ensure_user_state_schema(con)
    changed |= purge_orphaned_user_data(con)
    changed |= ensure_runtime_indexes(con)
    if changed:
        con.commit()
    return con


def prepare_database(db_path=None):
    path = resolve_db_path(db_path)
    ensure_base_database(path)
    maybe_apply_database_migrations(path)
    con = connect(path)
    try:
        con.commit()
    finally:
        con.close()
    return path


def rows_to_dicts(rows):
    return [dict(row) for row in rows]


def normalize_state(row=None):
    if row is None:
        return {
            "is_favorite": False,
            "progress_episode_number": None,
            "watched": False,
            "updated_at": None,
        }
    return {
        "is_favorite": bool(row["is_favorite"]),
        "progress_episode_number": row["progress_episode_number"],
        "watched": bool(row["watched"]),
        "updated_at": row["updated_at"],
    }


def apply_state_fields(item):
    item["is_favorite"] = bool(item.get("is_favorite") or 0)
    item["watched"] = bool(item.get("watched") or 0)
    item["progress_episode_number"] = item.get("progress_episode_number")
    item["state_updated_at"] = item.get("state_updated_at")
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


def normalize_key(value):
    text = str(value or "").strip().casefold().replace("ё", "е").replace("э", "е")
    return "".join(char for char in unicodedata.normalize("NFKD", text) if not unicodedata.combining(char))


def normalize_match_title(value):
    text = normalize_key(value)
    text = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def fold_search_text(value):
    text = value
    for pattern, replacement in SEARCH_FOLDS:
        text = pattern.sub(replacement, text)
    return text


def normalize_search_text(value):
    return fold_search_text(normalize_match_title(value))


def search_tokens(value):
    return [token for token in normalize_search_text(value).split() if token]


def unique_search_tokens(tokens):
    return list(dict.fromkeys(tokens))


def search_query_info(value):
    text = normalize_search_text(value)
    return {
        "text": text,
        "tokens": unique_search_tokens(token for token in text.split() if token),
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


def catalog_search_indexes(cache):
    indexes = cache.get("search_indexes")
    if indexes is not None:
        return indexes

    built = {item["id"]: item_search_index(item) for item in cache["items"]}
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


def variant_from_item(item):
    return {
        "id": item["id"],
        "source": item.get("source"),
        "source_id": item.get("source_id"),
        "title": item.get("title"),
        "subtitle": item.get("subtitle"),
        "url": item.get("url"),
        "year": item.get("year"),
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
        """
        select anime_id, label, value
        from anime_fields
        where value is not null
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
    item["is_favorite"] = any(bool(variant.get("is_favorite")) for variant in variants)
    item["watched"] = any(bool(variant.get("watched")) for variant in variants)
    progress_values = [
        variant.get("progress_episode_number")
        for variant in variants
        if variant.get("progress_episode_number") is not None
    ]
    item["progress_episode_number"] = max(progress_values) if progress_values else None
    item["state_updated_at"] = max(
        (variant.get("state_updated_at") for variant in variants if variant.get("state_updated_at")),
        default=None,
    )
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

    return aggregate_item_state(merged, sorted_items)


def can_auto_merge_by_title(bucket):
    source_counts = {}
    for item in bucket:
        source_counts[item.get("source")] = source_counts.get(item.get("source"), 0) + 1
    title_key = normalize_match_title(bucket[0].get("title"))
    subtitle_keys = {normalize_match_title(item.get("subtitle")) for item in bucket if normalize_match_title(item.get("subtitle"))}
    short_title_has_matching_subtitle = len(title_key) >= 8 or len(subtitle_keys) == 1
    return len(source_counts) > 1 and all(count == 1 for count in source_counts.values()) and short_title_has_matching_subtitle


def can_auto_merge_by_subtitle(bucket):
    source_counts = {}
    for item in bucket:
        source_counts[item.get("source")] = source_counts.get(item.get("source"), 0) + 1
    return len(source_counts) > 1 and all(count == 1 for count in source_counts.values())


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


def canonicalize_items(items):
    groups, remaining = merge_by_match_key(items, canonical_title_match_key, can_auto_merge_by_title)
    subtitle_groups, remaining = merge_by_match_key(remaining, canonical_subtitle_match_key, can_auto_merge_by_subtitle)
    groups.extend(subtitle_groups)
    groups.extend(merge_canonical_items([item]) for item in remaining)

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


def recent_update_summary(events):
    if not events:
        return None
    counts = {}
    for event in events:
        counts[event["event_type"]] = counts.get(event["event_type"], 0) + 1

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
        "days": content_updates.RECENT_UPDATE_DAYS,
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
    if item.get("is_favorite"):
        return 2.0
    if item.get("watched"):
        return 1.1
    if item.get("progress_episode_number") is not None:
        return 0.8
    return 0.0


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
        reasons.append(f"Близко к избранному: {titles}")

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


def get_recommendations(db_path=None, limit=DEFAULT_RECOMMENDATION_LIMIT, user_id=None):
    limit = normalize_recommendation_limit(limit or DEFAULT_RECOMMENDATION_LIMIT)
    items = get_anime_list(db_path, user_id=user_id)
    profile = build_recommendation_profile(items)
    has_profile = profile["seed_count"] > 0
    candidate_items = [
        item
        for item in items
        if not item.get("is_favorite")
        and not item.get("watched")
        and item.get("progress_episode_number") is None
    ]
    watchable_candidate_count = sum(1 for item in candidate_items if has_playable_source(item))
    has_watchable_candidates = watchable_candidate_count > 0
    recommendations = []

    for item in candidate_items:
        genre_score, matched_genres = genre_profile_score(item, profile)
        seed_score, based_on = seed_similarity(item, profile)
        taste_score = (0.7 * genre_score) + (0.3 * seed_score)
        quality = quality_score(item)
        popularity = popularity_score(item)
        availability = availability_score(item)
        recency = recency_score(item)
        kind_score = kind_profile_score(item, profile)

        if has_profile:
            raw_score = (
                (0.50 * taste_score)
                + (0.20 * quality)
                + (0.13 * availability)
                + (0.08 * popularity)
                + (0.05 * recency)
                + (0.04 * kind_score)
            )
        else:
            raw_score = (
                (0.36 * quality)
                + (0.30 * availability)
                + (0.18 * popularity)
                + (0.12 * recency)
                + (0.04 * kind_score)
            )

        score = watchable_recommendation_score(raw_score, item, has_watchable_candidates)
        item = public_item_copy(item)
        item["recommendation_score"] = score
        item["recommendation_confidence"] = recommendation_confidence(score)
        item["recommendation_matched_genres"] = matched_genres[:6]
        item["recommendation_based_on"] = based_on
        item["recommendation_reasons"] = recommendation_reasons(item, matched_genres, based_on)
        item["recommendation_components"] = {
            "taste": round(taste_score, 3),
            "quality": round(quality, 3),
            "availability": round(availability, 3),
            "popularity": round(popularity, 3),
            "recency": round(recency, 3),
            "watchable": 1.0 if has_playable_source(item) else 0.0,
            "raw": round(clamp(raw_score), 3),
        }
        recommendations.append(item)

    recommendations.sort(
        key=lambda item: (
            -item["recommendation_score"],
            -(numeric(item.get("source_count")) or 0),
            -(best_score(item) or 0),
            item["title"],
        )
    )
    recommendations = recommendations[:limit]
    for index, item in enumerate(recommendations, start=1):
        item["recommendation_rank"] = index

    return {
        "items": recommendations,
        "limit": limit,
        "profile": {
            "favorite_count": profile["favorite_count"],
            "seed_count": profile["seed_count"],
            "candidate_count": len(candidate_items),
            "watchable_candidate_count": watchable_candidate_count,
            "top_genres": profile["top_genres"],
            "mode": "personalized" if has_profile else "popular",
        },
    }


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


def get_source_anime_items(con, user_id=None):
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
            coalesce(us.is_favorite, 0) as is_favorite,
            us.progress_episode_number,
            coalesce(us.watched, 0) as watched,
            us.updated_at as state_updated_at,
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
    title_alias_search_fields = load_title_alias_search_fields(con)
    metadata_search_fields = load_metadata_search_fields(con)
    for item in items:
        apply_state_fields(item)
        item["genres"] = [g for g in (item.pop("genres") or "").split(",") if g]
        item["available_episode_count"] = item["available_episode_count"] or 0
        item["search_fields"] = unique_structured_search_fields(
            (title_alias_search_fields.get(item["id"]) or [])
            + (metadata_search_fields.get(item["id"]) or [])
        )
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


def get_anime_list(db_path=None, q=None, user_id=None):
    cache = get_catalog_cache(db_path, user_id)
    items = cache["items"]
    if not q:
        return clone_catalog_items(items)
    query = search_query_info(q)
    search_indexes = catalog_search_indexes(cache)
    scored = [
        (search_index_score(search_indexes.get(item["id"]) or item_search_index(item), query), index, item)
        for index, item in enumerate(items)
    ]
    return [
        clone_catalog_item(item)
        for score, _, item in sorted(scored, key=lambda entry: (-entry[0], entry[1]))
        if score > 0
    ]


def sql_placeholders(values):
    return ",".join("?" for _ in values)


def clone_catalog_item(item):
    cloned = dict(item)
    cloned["genres"] = list(item.get("genres") or [])
    cloned["sources"] = list(item.get("sources") or [])
    cloned["source_member_ids"] = list(item.get("source_member_ids") or [])
    cloned["source_variants"] = [dict(variant) for variant in item.get("source_variants") or []]
    cloned["search_fields"] = [dict(field) for field in item.get("search_fields") or []]
    cloned["recent_updates"] = [dict(update) for update in item.get("recent_updates") or []]
    cloned["recent_update_summary"] = dict(item["recent_update_summary"]) if item.get("recent_update_summary") else None
    return cloned


def clone_catalog_items(items):
    return [clone_catalog_item(item) for item in items]


def build_catalog_cache(db_path=None, user_id=None):
    path = resolve_db_path(db_path)
    con = connect(path)
    try:
        user_id = resolved_user_id(con, user_id)
        items = canonicalize_items(get_source_anime_items(con, user_id))
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

    return {
        "signature": db_signature(path),
        "items": items,
        "id_map": id_map,
        "slug_map": slug_map,
        "translation_rankings": translation_rankings,
    }


def get_catalog_cache(db_path=None, user_id=None):
    path = resolve_db_path(db_path)
    key = (str(path.resolve()), int(user_id) if user_id is not None else None)
    signature = db_signature(path)
    with CATALOG_CACHE_LOCK:
        cached = CATALOG_CACHE.get(key)
        if cached and cached.get("signature") == signature:
            return cached
        cached = build_catalog_cache(path, user_id)
        CATALOG_CACHE[key] = cached
        return cached


def invalidate_catalog_cache(db_path=None):
    path = resolve_db_path(db_path)
    prefix = str(path.resolve())
    with CATALOG_CACHE_LOCK:
        for key in list(CATALOG_CACHE):
            if key == prefix or (isinstance(key, tuple) and key[0] == prefix):
                CATALOG_CACHE.pop(key, None)


def get_catalog_items(db_path=None, user_id=None):
    return get_catalog_cache(db_path, user_id)["items"]


def canonical_group_for_anime_id(con, anime_id, user_id=None):
    db_path = con.execute("pragma database_list").fetchone()["file"]
    return get_catalog_cache(db_path, user_id)["id_map"].get(int(anime_id))


def canonical_group_for_anime_ref(con, anime_ref, user_id=None):
    value = str(anime_ref or "").strip()
    if not value:
        return None
    if value.isdigit():
        return canonical_group_for_anime_id(con, int(value), user_id)
    db_path = con.execute("pragma database_list").fetchone()["file"]
    return get_catalog_cache(db_path, user_id)["slug_map"].get(value)


def aggregate_state_rows(rows):
    if not rows:
        return normalize_state(None)
    progress_values = [
        row["progress_episode_number"]
        for row in rows
        if row["progress_episode_number"] is not None
    ]
    return {
        "is_favorite": any(bool(row["is_favorite"]) for row in rows),
        "progress_episode_number": max(progress_values) if progress_values else None,
        "watched": any(bool(row["watched"]) for row in rows),
        "updated_at": max((row["updated_at"] for row in rows if row["updated_at"]), default=None),
    }


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

    for episode_id, sources in by_episode.items():
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
    return detail


def update_user_state(anime_ref, patch, db_path=None, user_id=None):
    con = connect(db_path)
    try:
        user_id = require_user_id(con, user_id)
        group = canonical_group_for_anime_ref(con, anime_ref, user_id)
        if not group:
            con.close()
            return None
    except Exception:
        con.close()
        raise

    target_id = group["id"]
    member_ids = [variant["id"] for variant in group.get("source_variants") or []]
    current = get_group_state(con, member_ids, user_id)
    next_state = dict(current)

    if "is_favorite" in patch:
        next_state["is_favorite"] = bool(patch["is_favorite"])
    if "watched" in patch:
        next_state["watched"] = bool(patch["watched"])
    if "progress_episode_number" in patch:
        raw_value = patch["progress_episode_number"]
        if raw_value in (None, ""):
            next_state["progress_episode_number"] = None
        else:
            try:
                next_state["progress_episode_number"] = max(0, int(raw_value))
            except (TypeError, ValueError):
                con.close()
                raise ValueError("progress_episode_number must be a non-negative integer")

    next_state["updated_at"] = now_iso()
    con.execute(
        """
        insert into user_title_state (
            user_id,
            anime_id,
            is_favorite,
            progress_episode_number,
            watched,
            updated_at
        )
        values (?, ?, ?, ?, ?, ?)
        on conflict(user_id, anime_id) do update set
            is_favorite = excluded.is_favorite,
            progress_episode_number = excluded.progress_episode_number,
            watched = excluded.watched,
            updated_at = excluded.updated_at
        """,
        (
            user_id,
            target_id,
            1 if next_state["is_favorite"] else 0,
            next_state["progress_episode_number"],
            1 if next_state["watched"] else 0,
            next_state["updated_at"],
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
    con.commit()
    con.close()
    invalidate_catalog_cache(db_path)
    return next_state


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
        row = con.execute(
            """
            select u.*
            from sessions s
            join users u on u.id = s.user_id
            where s.token_hash = ?
              and s.revoked_at is null
              and s.expires_at > ?
            """,
            (session_token_hash(token), now_iso()),
        ).fetchone()
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


def run_content_sync(db_path, mode="daily", trigger="internal-api"):
    if mode not in SYNC_MODES:
        raise ValueError(f"unsupported sync mode: {mode}")

    import sync_videos

    started = time.perf_counter()
    args = sync_videos.parse_args(
        [
            "--db",
            str(db_path),
            "--mode",
            mode,
            "--source",
            "yummyanime",
            "--source",
            "animego",
            "--wait-lock",
            "--trigger",
            trigger,
        ]
    )
    try:
        stats = sync_videos.run_sync(args)
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
    event = {
        "event": "content_sync",
        "mode": mode,
        "trigger": trigger,
        "duration_ms": duration_ms,
        "stats": stats,
        "timestamp": now_iso(),
    }
    server_logger().info(json.dumps(event, ensure_ascii=False, sort_keys=True))
    invalidate_catalog_cache(db_path)
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
        scheduled_at = next_daily_sync_run(hour=hour, minute=minute)
        server_logger().info(
            json.dumps(
                {
                    "event": "content_sync_scheduler_scheduled",
                    "scheduled_at": scheduled_at.isoformat(timespec="seconds"),
                    "hour_utc": hour,
                    "minute_utc": minute,
                    "timestamp": now_iso(),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        sleep_until(scheduled_at)
        mode = os.environ.get("ANIME_SYNC_MODE", "daily").strip() or "daily"
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
        except Exception:
            server_logger().exception("content sync scheduler failed")


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
    parsed = urlparse(value or "/")
    if parsed.scheme or parsed.netloc:
        return "/"
    path = parsed.path or "/"
    if not path.startswith("/"):
        return "/"
    query = f"?{parsed.query}" if parsed.query else ""
    fragment = f"#{parsed.fragment}" if parsed.fragment else ""
    return f"{path}{query}{fragment}"


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
    signature = hmac.new(GOOGLE_AUTH_STATE_SECRET, payload_bytes, hashlib.sha256).digest()
    return f"{base64url_encode(payload_bytes)}.{base64url_encode(signature)}"


def verify_google_auth_state(value):
    try:
        payload_part, signature_part = (value or "").split(".", 1)
        payload_bytes = base64url_decode(payload_part)
        signature = base64url_decode(signature_part)
    except (ValueError, TypeError, binascii.Error):
        raise AuthError(GOOGLE_AUTH_STATE_ERROR)

    expected = hmac.new(GOOGLE_AUTH_STATE_SECRET, payload_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise AuthError(GOOGLE_AUTH_STATE_ERROR)

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
        issued_at = int(payload.get("iat") or 0)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise AuthError(GOOGLE_AUTH_STATE_ERROR)

    now = int(time.time())
    if issued_at < now - GOOGLE_AUTH_STATE_TTL_SECONDS or issued_at > now + 60:
        raise AuthError(GOOGLE_AUTH_STATE_ERROR)
    return safe_next_path(payload.get("next") or "/")


def create_login_handoff(session_token, next_path):
    code = secrets.token_urlsafe(32)
    now = time.time()
    with LOGIN_HANDOFFS_LOCK:
        expired_codes = [
            item_code
            for item_code, item in LOGIN_HANDOFFS.items()
            if item["expires_at"] <= now
        ]
        for item_code in expired_codes:
            LOGIN_HANDOFFS.pop(item_code, None)
        LOGIN_HANDOFFS[code] = {
            "expires_at": now + LOGIN_HANDOFF_TTL_SECONDS,
            "next_path": safe_next_path(next_path),
            "session_token": session_token,
        }
    return code


def consume_login_handoff(code):
    with LOGIN_HANDOFFS_LOCK:
        item = LOGIN_HANDOFFS.pop(code or "", None)
    if not item or item["expires_at"] <= time.time():
        raise AuthError("Сессия входа истекла. Попробуйте войти еще раз.")
    return item["session_token"], item["next_path"]


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

    def send_json(self, payload, status=200, headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._last_response_bytes = len(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or []):
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html_body, status=200, headers=None):
        body = html_body.encode("utf-8")
        self._last_response_bytes = len(body)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
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
        target = (STATIC_DIR / safe_path).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
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
        if safe_path == "login.html":
            self.send_header("Cross-Origin-Opener-Policy", "same-origin-allow-popups")
            self.send_header("Referrer-Policy", "no-referrer-when-downgrade")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def session_token(self):
        cookie_header = self.headers.get("Cookie")
        if not cookie_header:
            return None
        cookie = SimpleCookie()
        cookie.load(cookie_header)
        morsel = cookie.get(SESSION_COOKIE_NAME)
        return morsel.value if morsel else None

    def current_user(self):
        if hasattr(self, "_current_user"):
            return self._current_user
        self._current_user = get_session_user(self.session_token(), self.server.db_path)
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
        next_js = json.dumps(next_path, ensure_ascii=False)
        next_href = html.escape(next_path, quote=True)
        recovery_path = f"/login?{urlencode({'next': next_path, 'auth_complete': '1'})}"
        recovery_js = json.dumps(recovery_path, ensure_ascii=False)
        self.send_html(
            f"""<!doctype html>
<html lang=\"ru\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Вход выполнен - Anime Catalog</title>
</head>
<body>
  <p id=\"login-complete-state\">Вход выполнен. Открываю приложение...</p>
  <p><a href=\"{next_href}\">Открыть приложение</a></p>
  <script>
    const nextPath = {next_js};
    const state = document.getElementById("login-complete-state");
    const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

    async function waitForSession() {{
      for (let attempt = 0; attempt < 30; attempt += 1) {{
        try {{
          const response = await fetch("/api/me", {{
            cache: "no-store",
            credentials: "same-origin",
          }});
          if (response.ok) {{
            window.location.replace(nextPath);
            return;
          }}
        }} catch (error) {{
          // The next retry handles transient navigation/cookie timing.
        }}
        await delay(100);
      }}
      state.textContent = "Вход выполнен. Открываю приложение...";
      window.location.replace({recovery_js});
    }}

    waitForSession();
  </script>
</body>
</html>
""",
            headers=[("Set-Cookie", self.session_cookie_header(token))],
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
            self.send_json({"ok": True})
            return

        if path == "/api/auth/config":
            client_id = google_client_id()
            next_path = safe_next_path(parse_qs(parsed.query).get("next", ["/"])[0])
            self.send_json(
                {
                    "configured": bool(client_id),
                    "client_id": client_id,
                    "state": sign_google_auth_state(next_path) if client_id else "",
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
                    parse_qs(parsed.query).get("code", [""])[0]
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

        if path == "/api/admin/users":
            if self.require_admin():
                self.send_json(admin_users_payload(self.server.db_path))
            return

        if path == "/api/anime":
            query = parse_qs(parsed.query).get("q", [""])[0].strip()
            self.send_json({"items": get_anime_list(self.server.db_path, query or None, user["id"])})
            return

        if path == "/api/recommendations":
            raw_limit = parse_qs(parsed.query).get("limit", [str(DEFAULT_RECOMMENDATION_LIMIT)])[0]
            self.send_json(get_recommendations(self.server.db_path, raw_limit, user["id"]))
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
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
                return
            except Exception as exc:
                server_logger().exception("content sync failed")
                self.send_json({"error": str(exc) or "sync failed"}, 500)
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
            code = create_login_handoff(auth["token"], next_path)
            complete_url = f"/api/auth/complete?code={quote(code, safe='')}"
            if is_redirect_flow:
                self.send_redirect(complete_url)
                return
            self.send_json(
                {"user": auth["user"], "complete_url": complete_url},
            )
            return

        if path == "/api/logout":
            revoke_session(self.session_token(), self.server.db_path)
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
