# Task Changelog: Project Hardening

Append-only implementation and validation diary for the repository-wide
hardening pass.

**Started:** 2026-07-09
**Status:** COMPLETE

---

## [2026-07-09 22:10] - Codex GPT-5

**Context:** Closing the repository-wide implementation and verification pass.

**Action:** Hardened canonical merging, request/cache/readiness concurrency,
auth handoff and state signing, strict user/watch mutations, frontend request
ordering and save queues, watch/login lifecycle, scraper failure semantics,
shared DB/publication locks, URL validation, batch-atomic migrations, complete
recovery exports, Railway missing-DB handoff, and cron fail-closed behavior.
Added deterministic fixtures, CI, pinned tooling, schema/index/integrity
migrations, current runbooks, and a partial covering external-rating index.

**Result:** A fresh checkout with a fresh virtualenv passes 165 Python tests,
Node runtime tests, JavaScript syntax, strict TypeScript compilation, Ruff,
dependency audit, shell syntax, compilation, and repository hygiene without
`db/animego.sqlite`. Dev has 4,362 source titles, 63,076 episodes, 190,597
playable sources, zero FK errors, valid current checksums, and a successfully
restored real user-state export. Browser smoke passes anonymous login, desktop
catalog/search/detail/state/player/history, and 390x844 mobile scrolling with no
horizontal overflow. Production was not touched.

**Performance:** Canonicalization of a 2,000-row synthetic catalog improved
about 9x; cache revision reads with a request connection about 30x; external
rating loading on Dev improved from a 48.4 ms full-table median to 2.4 ms via a
151 KB partial covering index. Migration copies dropped from two to one and
full integrity scans from three to two in the normal verified backup path.

**Residual constraint:** Cross-origin players do not expose their internal
pause/time state to the parent page, so automatic watch time remains a bounded
iframe-focus/fullscreen evidence heuristic; manual progress remains the source
of correction.

---

## [2026-07-09 21:00] - Codex GPT-5

**Context:** Starting the implementation pass after the repository review.

**Action:** Captured baseline commit `c35b362`, created a disposable SQLite
backup in `/tmp`, split changes into non-overlapping backend, frontend, and
data/operations batches, pinned dependencies, and added a clean-checkout CI
workflow.

**Result:** Baseline tests pass only with the mutable development database. A
clean archive reproduces 10 failures and 14 errors, confirming the need for a
deterministic fixture. The baseline database passes `integrity_check` but has 20
foreign-key violations.

**Next Steps:** Complete the three implementation batches, repair the dev
database, run full tests and browser checks, then close this task with measured
results.

---
