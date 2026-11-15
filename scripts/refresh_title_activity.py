#!/usr/bin/env python3
"""Refresh available upstream title dates without changing catalog content."""

import argparse
import datetime as dt
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import title_activity  # noqa: E402
from scripts.operation_lock import DatabaseOperationLock  # noqa: E402


def refresh(db_path):
    observed_at = dt.datetime.now(dt.timezone.utc)
    snapshots = title_activity.fetch_source_snapshots(observed_at)
    with DatabaseOperationLock(db_path, wait=True, timeout=30, operation="refresh title activity dates"):
        con = sqlite3.connect(db_path)
        try:
            con.execute("pragma foreign_keys=on")
            con.execute("begin immediate")
            title_activity.ensure_schema(con)
            matched = title_activity.apply_source_snapshots(con, snapshots, observed_at)
            con.commit()
            return matched
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    args = parser.parse_args()
    matched = refresh(args.db)
    print(f"source dates refreshed for {matched} catalog entries")


if __name__ == "__main__":
    main()
