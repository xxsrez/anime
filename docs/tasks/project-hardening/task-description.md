# Task Description: Project Hardening

**Created:** 2026-07-09
**Author:** Codex GPT-5

## Problem Statement

Perform a complete engineering hardening pass instead of stopping at a review.
Fix confirmed bugs and races, improve failure semantics and performance, repair
the development database, and make verification reproducible from a clean
checkout.

## Confirmed Symptoms

- Partial content syncs can update `last_success` and return HTTP success.
- Canonical-title transitivity can merge unrelated titles.
- Runtime SQLite connections do not enforce declared foreign keys.
- Concurrent state writes and stale frontend responses can lose user changes.
- Backup and multi-file migration workflows are not atomic as a logical unit.
- Readiness can report success without a usable database.
- The test suite relies on the ignored mutable development database.
- Several frontend timers and catalog operations perform unbounded work.

## Environment

- Development checkout: `/Users/andrey/Projects/Home/Anime`
- Development URL: `http://127.0.0.1:8765/`
- Development database: `db/animego.sqlite`
- Production: Railway, explicitly excluded from changes in this task

## Investigation Plan

- [x] Repair sync, migration, backup, and database invariants.
- [x] Repair backend concurrency, validation, cache, and readiness behavior.
- [x] Repair frontend request ordering, persistence, watch tracking, and login.
- [x] Add deterministic clean-checkout tests, CI, and dependency checks.
- [x] Repair and validate the development database.
- [x] Run full automated and browser verification and update documentation.

## Safety Constraints

- Keep a disposable pre-change copy of the development SQLite database.
- Do not call, deploy, restart, or mutate Railway production.
- Preserve source-specific rows and use conservative canonical matching.
- Never publish a recovery snapshot that fails database invariants.
