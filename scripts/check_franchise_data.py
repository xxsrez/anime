#!/usr/bin/env python3
"""Audit checked-in franchise definitions against a local catalog database."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "content" / "franchise-seeds.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import franchise_catalog  # noqa: E402
import server  # noqa: E402


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "db" / "animego.sqlite")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--expected-generated", type=int)
    parser.add_argument(
        "--review-limit",
        type=int,
        default=20,
        help="include this many highest-rated uncovered catalog titles in the review queue",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    expected_generated = args.expected_generated
    if expected_generated is None:
        expected_generated = len(manifest.get("items") or [])
    definitions = franchise_catalog.load_definitions()
    generated = [
        definition
        for definition in definitions
        if (definition.get("data_origin") or {}).get("provider") == "shikimori"
    ]
    if len(generated) != expected_generated:
        raise SystemExit(
            f"expected {expected_generated} generated franchises, found {len(generated)}"
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

    catalog_items = server.get_anime_list(args.db)
    uncovered = [item for item in catalog_items if int(item["id"]) not in canonical_owners]
    uncovered.sort(
        key=lambda item: (
            -(server.numeric(item.get("effective_score")) or 0),
            -(server.numeric(item.get("aggregate_count")) or 0),
            str(item.get("title") or "").casefold(),
            int(item["id"]),
        )
    )
    uncovered_title_review_queue = [
        {
            "id": int(item["id"]),
            "title": item.get("title"),
            "subtitle": item.get("subtitle"),
            "year": item.get("year"),
            "score": item.get("effective_score"),
            "votes": int(server.numeric(item.get("aggregate_count")) or 0),
        }
        for item in uncovered[: max(0, args.review_limit)]
    ]

    print(
        json.dumps(
            {
                "definitions": len(definitions),
                "generated": len(generated),
                "entries": sum(row["entries"] for row in franchise_rows),
                "available_entries": sum(row["available"] for row in franchise_rows),
                "unique_canonical_groups": len(canonical_owners),
                "catalog_groups": len(catalog_items),
                "minimum_generated_coverage": min(
                    row["available"]
                    for row in franchise_rows
                    if row["slug"] in {definition["slug"] for definition in generated}
                ),
                "franchises": franchise_rows,
                "uncovered_title_review_queue": uncovered_title_review_queue,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
