#!/usr/bin/env python3
import json
import os
from pathlib import Path
import time
from urllib import error, request


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
        return explicit
    base = os.environ.get("ANIME_PUBLIC_URL", "").strip().rstrip("/")
    if base:
        return f"{base}/api/internal/daily-sync"
    raise RuntimeError("ANIME_SYNC_URL or ANIME_PUBLIC_URL must be configured")


def main():
    url = sync_url()
    mode = os.environ.get("ANIME_SYNC_MODE", "daily").strip() or "daily"
    token = os.environ.get("ANIME_SYNC_TOKEN", "").strip()
    if not token:
        raise RuntimeError("ANIME_SYNC_TOKEN must be configured")

    separator = "&" if "?" in url else "?"
    target = f"{url}{separator}mode={mode}"
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
        with request.urlopen(req, timeout=timeout) as response:
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
            if response.status >= 400 or not payload.get("ok"):
                return 1
            return 0
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
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
