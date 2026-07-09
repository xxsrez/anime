# Task: Project Hardening

**Status:** COMPLETE
**Priority:** High
**Started:** 2026-07-09

## Problem

The application works on the primary happy paths, but a repository-wide review
found correctness, concurrency, recovery, scheduling, performance, security,
and reproducibility gaps. Several failures can be reported as success, local
state updates can race, and clean-clone verification depends on an ignored
database.

## Goal

Make the development checkout reliable and reproducible: enforce database
invariants, make cron and recovery fail closed, remove confirmed backend and
frontend races, bound expensive work, and cover the repaired behavior with
tests and operational documentation.

## Scope

**In scope:**

- Backend, frontend, scraper, sync, migration, backup, and health-check fixes.
- Deterministic tests that work without the mutable development database.
- Local development database repair and browser verification on port `8765`.
- CI, dependency pinning, and current runbook updates.

**Out of scope:**

- Any Railway production mutation or deployment.
- Treating expected production/development catalog freshness differences as a
  defect.
- Replacing third-party playback providers.

## Files

- `changelog.md` - append-only implementation and validation diary.
- `task-description.md` - original problem and phased investigation plan.

## Related

- `docs/instructions/Operations_Runbook.md`
- `docs/instructions/Testing_Plan.md`
- `docs/architecture.md`

## Outcome

Completed on 2026-07-09. The clean-checkout gate passes without the ignored Dev
database, Dev migrations and recovery artifacts are current and healthy, and
desktop/mobile browser smoke tests pass. Railway production was not accessed or
changed; its cron-driven freshness remains intentionally independent from Dev.
