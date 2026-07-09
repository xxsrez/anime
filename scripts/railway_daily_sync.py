#!/usr/bin/env python3
import json
import os
from pathlib import Path
import time
from urllib import error, request
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    from scripts.http_safety import open_validated_url, validate_http_url
except ModuleNotFoundError:  # Direct execution: python3 scripts/railway_daily_sync.py
    from http_safety import open_validated_url, validate_http_url


DEFAULT_TIMEOUT_SECONDS = 60 * 30


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log_event(event):
    payload = {
        "timestamp": now_iso(),
        **event,
    }
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    print(line, flush=True)
    log_path = os.environ.get("ANIME_CRON_LOG_PATH", "").strip()
    if log_path:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def sync_url():
    explicit = os.environ.get("ANIME_SYNC_URL", "").strip()
    if explicit:
        return validate_http_url(explicit, allow_local_http=True)
    base = os.environ.get("ANIME_PUBLIC_URL", "").strip().rstrip("/")
    if base:
        return validate_http_url(f"{base}/api/internal/daily-sync", allow_local_http=True)
    raise RuntimeError("ANIME_SYNC_URL or ANIME_PUBLIC_URL must be configured")


def contains_failure(value, depth=0):
    if depth > 12:
        return True
    if isinstance(value, dict):
        if value.get("status") in {"partial", "failed"} or value.get("error"):
            return True
        if "failed" in value:
            try:
                if int(value["failed"] or 0) > 0:
                    return True
            except (TypeError, ValueError):
                return True
        return any(contains_failure(child, depth + 1) for child in value.values())
    if isinstance(value, list):
        return any(contains_failure(child, depth + 1) for child in value)
    return False


def sync_succeeded(payload):
    return (
        isinstance(payload, dict)
        and payload.get("ok") is True
        and not contains_failure(payload)
    )


def target_url(url, mode):
    parsed = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "mode"]
    query.append(("mode", mode))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def main():
    url = sync_url()
    mode = os.environ.get("ANIME_SYNC_MODE", "daily").strip() or "daily"
    if mode not in {"hourly", "daily", "full"}:
        raise RuntimeError(f"unsupported ANIME_SYNC_MODE: {mode}")
    token = os.environ.get("ANIME_SYNC_TOKEN", "").strip()
    if not token:
        raise RuntimeError("ANIME_SYNC_TOKEN must be configured")

    target = target_url(url, mode)
    timeout = int(os.environ.get("ANIME_SYNC_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    body = b"{}"
    req = request.Request(
        target,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        },
    )

    started = time.perf_counter()
    log_event({"event": "daily_sync_start", "mode": mode, "url": url})
    try:
        with open_validated_url(
            req,
            timeout=timeout,
            allowed_hosts=(urlsplit(target).hostname,),
            allow_local_http=True,
        ) as response:
            raw = response.read().decode("utf-8", "replace")
            duration_ms = max(0, int((time.perf_counter() - started) * 1000))
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"raw": raw[:2000]}
            log_event(
                {
                    "event": "daily_sync_finish",
                    "mode": mode,
                    "status": response.status,
                    "duration_ms": duration_ms,
                    "response": payload,
                }
            )
            if response.status >= 400 or not sync_succeeded(payload):
                return 1
            return 0
    except error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", "replace")
        finally:
            exc.close()
        duration_ms = max(0, int((time.perf_counter() - started) * 1000))
        log_event(
            {
                "event": "daily_sync_http_error",
                "mode": mode,
                "status": exc.code,
                "duration_ms": duration_ms,
                "response": raw[:2000],
            }
        )
        return 1
    except Exception as exc:
        duration_ms = max(0, int((time.perf_counter() - started) * 1000))
        log_event(
            {
                "event": "daily_sync_error",
                "mode": mode,
                "duration_ms": duration_ms,
                "error": str(exc),
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
