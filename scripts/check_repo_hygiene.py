#!/usr/bin/env python3
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAX_TRACKED_FILE_BYTES = 1_000_000

FORBIDDEN_PREFIXES = (
    "db/",
    "data/",
    "backups/",
    ".venv/",
    "__pycache__/",
)
FORBIDDEN_EXACT = {
    ".env",
}
FORBIDDEN_SUFFIXES = (
    ".sqlite",
    ".sqlite3",
    ".db",
    ".db3",
    ".pyc",
    ".pyo",
    ".log",
)


def git(*args):
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    return result.stdout.splitlines()


def is_forbidden_path(path):
    return (
        path in FORBIDDEN_EXACT
        or path.startswith(FORBIDDEN_PREFIXES)
        or path.endswith(FORBIDDEN_SUFFIXES)
        or (path.startswith("migrations/") and "data-update" in Path(path).name)
    )


def check_tracked_files():
    errors = []
    tracked = git("ls-files")
    for path in tracked:
        if is_forbidden_path(path):
            errors.append(f"forbidden tracked path: {path}")
            continue
        full_path = ROOT / path
        if full_path.is_file() and full_path.stat().st_size > MAX_TRACKED_FILE_BYTES:
            size_mb = full_path.stat().st_size / 1_000_000
            errors.append(f"tracked file is too large: {path} ({size_mb:.1f} MB)")
    return tracked, errors


def check_history_paths():
    errors = []
    paths = set(git("log", "--all", "--name-only", "--pretty=format:"))
    for path in sorted(item for item in paths if item):
        if is_forbidden_path(path):
            errors.append(f"forbidden path exists in git history: {path}")
    return errors


def check_internal_refs():
    refs = git("for-each-ref", "--format=%(refname)", "refs/codex/turn-diffs")
    return [f"forbidden local git ref: {ref}" for ref in refs]


def main():
    tracked, errors = check_tracked_files()
    errors.extend(check_history_paths())
    errors.extend(check_internal_refs())
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        raise SystemExit(1)
    print(f"repo hygiene ok: {len(tracked)} tracked files")


if __name__ == "__main__":
    main()
