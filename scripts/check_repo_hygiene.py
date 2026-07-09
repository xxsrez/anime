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
    # Include non-ignored untracked files so the gate catches a forbidden file
    # before it is staged, not only after it enters Git's index.
    repository_files = git("ls-files", "--cached", "--others", "--exclude-standard")
    for path in repository_files:
        if is_forbidden_path(path):
            errors.append(f"forbidden tracked path: {path}")
            continue
        full_path = ROOT / path
        if full_path.is_file() and full_path.stat().st_size > MAX_TRACKED_FILE_BYTES:
            size_mb = full_path.stat().st_size / 1_000_000
            errors.append(f"tracked file is too large: {path} ({size_mb:.1f} MB)")
    return repository_files, errors


def check_history_paths():
    errors = []
    # Inspect refs that can be published. Codex keeps local turn snapshots in
    # refs/codex/turn-diffs; those are application-owned implementation refs,
    # are not pushed by --all, and must not make the repository's own gate
    # impossible to run from Codex.
    paths = set(
        git(
            "log",
            "--branches",
            "--remotes",
            "--tags",
            "--name-only",
            "--pretty=format:",
        )
    )
    for path in sorted(item for item in paths if item):
        if is_forbidden_path(path):
            errors.append(f"forbidden path exists in git history: {path}")
    return errors


def main():
    repository_files, errors = check_tracked_files()
    errors.extend(check_history_paths())
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        raise SystemExit(1)
    print(f"repo hygiene ok: {len(repository_files)} tracked or candidate files")


if __name__ == "__main__":
    main()
