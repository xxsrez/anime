#!/usr/bin/env python3
"""Collect AnimeGO updates from an allowed egress and push validated snapshots to production."""

import argparse
from collections import OrderedDict, defaultdict
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sync_videos  # noqa: E402


DEFAULT_PRODUCTION_URL = "https://anime-srez.up.railway.app"
DEFAULT_MINIMUM_INTERVAL_HOURS = 20.0
DEFAULT_MAX_RUNTIME_SECONDS = 30 * 60


class RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_https_url(url, *, require_origin=False):
    parsed = urlsplit(str(url))
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("trusted AnimeGO worker requires an HTTPS production URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("production URL credentials are not allowed")
    if require_origin and (parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("production URL must be an origin without a path, query, or fragment")
    return str(url).rstrip("/")


def bearer_request(url, token, *, payload=None, timeout=60, opener=None):
    url = validate_https_url(url)
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "AnimeGO trusted push worker/1",
    }
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
        headers["Content-Length"] = str(len(data))
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    opener = opener or urllib.request.build_opener(RejectRedirectHandler())
    with opener.open(request, timeout=timeout) as response:
        if response.geturl() != url:
            raise ValueError("trusted AnimeGO worker refused a redirected response")
        body = response.read()
        return response.status, json.loads(body.decode("utf-8"))


def railway_push_token(railway_cli):
    configured = os.environ.get("ANIMEGO_PUSH_TOKEN", "").strip()
    if configured:
        return configured
    command = [
        railway_cli,
        "variable",
        "list",
        "--service",
        "web",
        "--environment",
        "production",
        "--json",
    ]
    completed = subprocess.run(command, cwd=ROOT, check=False, capture_output=True, text=True, timeout=30)
    if completed.returncode:
        message = (completed.stderr or "Railway variable lookup failed").strip().splitlines()[-1]
        raise RuntimeError(message)
    payload = json.loads(completed.stdout)
    token = str(payload.get("ANIMEGO_PUSH_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("ANIMEGO_PUSH_TOKEN is not configured on Railway web")
    return token


def parse_timestamp(value):
    if not value:
        return None
    try:
        timestamp = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=dt.timezone.utc)
    return timestamp.astimezone(dt.timezone.utc)


def sync_is_fresh(manifest, mode, minimum_interval_hours, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    state = manifest.get("sync_state") or {}
    last_success = parse_timestamp(state.get(f"animego:{mode}:last_success"))
    if last_success is None:
        return False
    return (now - last_success).total_seconds() < max(0.0, minimum_interval_hours) * 3600


def episode_number(value):
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return -1.0


def incremental_episode_selector(playable_episode_ids, refresh_recent):
    playable = {int(value) for value in playable_episode_ids}

    def select(episodes, selected_episode_id):
        ordered = sorted(
            episodes,
            key=lambda episode: (episode_number(episode.get("number")), int(episode.get("id") or 0)),
        )
        wanted = {int(episode["id"]) for episode in ordered if int(episode["id"]) not in playable}
        if refresh_recent > 0:
            wanted.update(int(episode["id"]) for episode in ordered[-refresh_recent:])
        if selected_episode_id is not None:
            wanted.add(int(selected_episode_id))
        return [episode for episode in episodes if int(episode["id"]) in wanted]

    return select


def worker_sync_args(args):
    argv = [
        "--mode",
        args.mode,
        "--source",
        "animego",
        "--animego-discover-pages",
        str(args.discover_pages),
        "--retry-attempts",
        str(args.retry_attempts),
        "--retry-backoff",
        str(args.retry_backoff),
        "--delay",
        str(args.delay),
        "--trigger",
        "trusted-animego-worker",
    ]
    return sync_videos.parse_args(argv)


def collect_bundle(manifest, args):
    collection_started_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    collection_started = time.perf_counter()
    sync_args = worker_sync_args(args)
    sync_args.deadline_monotonic = collection_started + args.max_runtime_seconds
    listing_stats = defaultdict(int)
    errors = []
    try:
        listing = sync_videos.collect_animego_listing(sync_args, listing_stats)
    except Exception as error:
        listing = []
        errors.append(f"listing: {type(error).__name__}: {error}")
    if listing_stats.get("listing_failed"):
        errors.append(
            f"listing: {int(listing_stats['listing_failed'])} request(s) failed after retries"
        )

    known = {int(entry["item"]["id"]): entry for entry in manifest.get("items") or []}
    candidates = OrderedDict()
    for item in listing:
        candidates[int(item["id"])] = item
    for anime_id, entry in known.items():
        candidates.setdefault(anime_id, entry["item"])
    total_candidates = len(candidates)
    if args.limit:
        candidates = OrderedDict(list(candidates.items())[: args.limit])
    selected_candidates = len(candidates)

    snapshots = []
    consecutive_source_failures = 0
    for index, (anime_id, item) in enumerate(candidates.items(), start=1):
        if time.monotonic() >= sync_args.deadline_monotonic:
            errors.append(f"collection deadline exceeded; skipped {len(candidates) - index + 1} titles")
            break
        print(f"[trusted animego {index}/{len(candidates)}] {item['title']}", flush=True)
        entry = known.get(anime_id)
        selector = None
        if args.mode == "hourly" and entry is not None:
            selector = incremental_episode_selector(entry.get("playable_episode_ids") or [], args.refresh_recent)
        try:
            snapshot = sync_videos.fetch_animego_snapshot(item, sync_args, episode_selector=selector)
            if snapshot is None:
                errors.append(f"{anime_id}: player not found")
                consecutive_source_failures = 0
                continue
            if not snapshot.get("episodes"):
                errors.append(f"{anime_id}: player exposed no episodes")
                consecutive_source_failures = 0
                continue
            invalid_empty = next(
                (
                    episode_snapshot
                    for episode_snapshot in snapshot["episodes"]
                    if not episode_snapshot.get("providers")
                    and not str(episode_snapshot.get("unavailable_reason") or "").strip()
                ),
                None,
            )
            if invalid_empty is not None:
                episode_id = invalid_empty.get("episode", {}).get("id")
                errors.append(f"{anime_id}: episode {episode_id} has no provider or unavailable reason")
                consecutive_source_failures = 0
                continue
            snapshots.append(snapshot)
            consecutive_source_failures = 0
        except Exception as error:
            errors.append(f"{anime_id}: {type(error).__name__}: {error}")
            if sync_videos.animego_source_unavailable_error(error):
                consecutive_source_failures += 1
            else:
                consecutive_source_failures = 0
            if consecutive_source_failures >= args.source_failure_threshold:
                skipped = len(candidates) - index
                if skipped:
                    errors.append(f"source circuit breaker skipped {skipped} titles")
                break

    episodes = sum(len(snapshot.get("episodes") or []) for snapshot in snapshots)
    providers = sum(
        len(episode.get("providers") or [])
        for snapshot in snapshots
        for episode in snapshot.get("episodes") or []
    )
    duration_ms = max(0, int((time.perf_counter() - collection_started) * 1000))
    return {
        "version": sync_videos.ANIMEGO_BUNDLE_VERSION,
        "source": "animego",
        "mode": args.mode,
        "collection_started_at": collection_started_at,
        "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "snapshots": snapshots,
        "errors": errors,
        "complete": not bool(args.limit),
        "collector": {
            "candidates": total_candidates,
            "selected_candidates": selected_candidates,
            "listing_candidates": len(listing),
            "listing_pages": int(listing_stats.get("listing_pages") or 0),
            "listing_failed": int(listing_stats.get("listing_failed") or 0),
            "snapshots": len(snapshots),
            "errors": len(errors),
            "episodes": episodes,
            "providers": providers,
            "duration_ms": duration_ms,
        },
    }


def bundle_summary(bundle):
    return dict(bundle.get("collector") or {})


def main(argv=None):
    run_started = time.perf_counter()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production-url", default=DEFAULT_PRODUCTION_URL)
    parser.add_argument("--railway-cli", default=shutil.which("railway") or "/opt/homebrew/bin/railway")
    parser.add_argument("--mode", choices=["hourly", "daily", "full"], default="daily")
    parser.add_argument("--minimum-interval-hours", type=float, default=DEFAULT_MINIMUM_INTERVAL_HOURS)
    parser.add_argument("--discover-pages", type=int)
    parser.add_argument("--refresh-recent", type=int, default=3)
    parser.add_argument("--retry-attempts", type=int, default=3)
    parser.add_argument("--retry-backoff", type=float, default=3.0)
    parser.add_argument("--source-failure-threshold", type=int, default=2)
    parser.add_argument("--max-runtime-seconds", type=float, default=DEFAULT_MAX_RUNTIME_SECONDS)
    parser.add_argument("--delay", type=float, default=0.25)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    args.production_url = validate_https_url(args.production_url, require_origin=True)
    if args.discover_pages is None:
        args.discover_pages = 1 if args.mode == "hourly" else 3 if args.mode == "daily" else 0
    if (
        args.discover_pages < 0
        or args.refresh_recent < 0
        or args.source_failure_threshold < 1
        or args.max_runtime_seconds <= 0
        or args.max_runtime_seconds > sync_videos.MAX_ANIMEGO_COLLECTION_DURATION.total_seconds()
    ):
        parser.error(
            "discover-pages and refresh-recent cannot be negative; "
            "source-failure-threshold must be positive; max-runtime-seconds must be between 1 and 2700"
        )

    token = railway_push_token(args.railway_cli)
    _status, manifest = bearer_request(
        f"{args.production_url}/api/internal/animego-sync-manifest",
        token,
        timeout=30,
    )
    if not args.force and sync_is_fresh(manifest, args.mode, args.minimum_interval_hours):
        print(
            json.dumps(
                {
                    "event": "animego_push_skip",
                    "reason": "fresh",
                    "mode": args.mode,
                    "total_duration_ms": max(0, int((time.perf_counter() - run_started) * 1000)),
                },
                sort_keys=True,
            )
        )
        return 0

    bundle = collect_bundle(manifest, args)
    sync_videos.validate_animego_bundle(bundle)
    summary = bundle_summary(bundle)
    print(json.dumps({"event": "animego_push_collected", **summary}, ensure_ascii=False, sort_keys=True))
    if args.output:
        target = Path(args.output).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        target.chmod(0o600)
    if args.dry_run:
        return 1 if bundle.get("errors") else 0

    try:
        status, response = bearer_request(
            f"{args.production_url}/api/internal/animego-push-sync",
            token,
            payload=bundle,
            timeout=15 * 60,
        )
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        try:
            response = json.loads(body)
        except json.JSONDecodeError:
            response = {"error": body[:500]}
        print(
            json.dumps(
                {
                    "event": "animego_push_failed",
                    "status": error.code,
                    "response": response,
                    "total_duration_ms": max(0, int((time.perf_counter() - run_started) * 1000)),
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "event": "animego_push_finished",
                "status": status,
                "response": response,
                "total_duration_ms": max(0, int((time.perf_counter() - run_started) * 1000)),
            },
            ensure_ascii=False,
        )
    )
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
