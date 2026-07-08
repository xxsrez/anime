# Database Migrations

This directory contains ordered SQLite migration scripts.

Rules:

- First-level directories are applied in lexicographic order.
- `.sql` files inside each directory are applied in lexicographic order.
- Applied scripts are tracked by relative path and SHA-256 checksum in
  `schema_migrations`.
- Already applied scripts are append-only. If a script needs a correction, add a
  new dated directory or a later file instead of editing the old script.
- Migration files must not contain `BEGIN`, `COMMIT`, `ROLLBACK`, `SAVEPOINT`,
  or `RELEASE`; the runner wraps each file in one transaction.

Example:

```text
migrations/
  2026-07-08_user-state-fix/
    00_fix-bad-progress.sql
    01_backfill-updated-at.sql
```
