#!/usr/bin/env python3
"""Audit checked-in franchise definitions against a local catalog database."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import franchise_catalog  # noqa: E402
import server  # noqa: E402


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "db" / "animego.sqlite")
    parser.add_argument("--expected-generated", type=int, default=40)
    return parser.parse_args()


def main():
    args = parse_args()
    definitions = franchise_catalog.load_definitions()
    generated = [
        definition
        for definition in definitions
        if (definition.get("data_origin") or {}).get("provider") == "shikimori"
    ]
    if len(generated) != args.expected_generated:
        raise SystemExit(
            f"expected {args.expected_generated} generated franchises, found {len(generated)}"
        )

    canonical_owners = {}
    franchise_rows = []
    for definition in definitions:
        payload = server.get_franchise_detail(definition["slug"], args.db)
        available_count = int(payload["available_count"])
        if definition in generated and available_count < 1:
            raise SystemExit(f"{definition['slug']} has no matching canonical catalog title")
        for entry in payload["entries"]:
            catalog_item = entry.get("catalog_item")
            if not catalog_item:
                continue
            group_id = int(catalog_item["id"])
            owner = f"{definition['slug']}/{entry['id']}"
            previous = canonical_owners.get(group_id)
            if previous and previous != owner:
                raise SystemExit(
                    f"canonical group {group_id} belongs to both {previous} and {owner}"
                )
            canonical_owners[group_id] = owner
        franchise_rows.append(
            {
                "slug": definition["slug"],
                "entries": int(payload["entry_count"]),
                "available": available_count,
                "active": sum(
                    entry.get("status") in {"announced", "releasing"}
                    for entry in payload["entries"]
                ),
            }
        )

    print(
        json.dumps(
            {
                "definitions": len(definitions),
                "generated": len(generated),
                "entries": sum(row["entries"] for row in franchise_rows),
                "available_entries": sum(row["available"] for row in franchise_rows),
                "unique_canonical_groups": len(canonical_owners),
                "minimum_generated_coverage": min(
                    row["available"]
                    for row in franchise_rows
                    if row["slug"] in {definition["slug"] for definition in generated}
                ),
                "franchises": franchise_rows,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
