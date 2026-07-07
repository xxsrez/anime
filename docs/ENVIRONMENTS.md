# Local Environments And Release Rules

This project uses two fixed localhost environments. Keep the split strict so
development work cannot break the stable local production site.

## Environment Map

| Environment | URL | Files | Database | Agent rule |
| --- | --- | --- | --- | --- |
| dev/test/scratch | `http://127.0.0.1:8765/` | `/Users/andrey/Projects/Home/Anime` | `/Users/andrey/Projects/Home/Anime/db/animego.sqlite` | Safe for regular edits, scraping, indexing, restarts, and browser testing. |
| prod | `http://127.0.0.1:8766/` | `/Users/andrey/.local/share/anime-local-prod` | `/Users/andrey/.local/share/anime-local-prod/shared/animego.sqlite` | Release-only. Do not edit, restart, scrape, index, or test against it unless the user explicitly asks for a prod operation. |

Port `8776` is retired for this project. Do not use it for Anime dev/prod work
unless the user explicitly changes the scheme again.

## Development

Run and test the current working tree on dev:

```bash
cp .env.example .env
printf 'GOOGLE_CLIENT_ID=%s\n' '...apps.googleusercontent.com' > .env
.venv/bin/python server.py --host 127.0.0.1 --port 8765
```

The Google OAuth client must be type `Web application`. Add the exact dev URL
origin you open in the browser to Authorized JavaScript origins, usually
`http://127.0.0.1:8765` and optionally `http://localhost:8765`. The normal
login button uses Google Identity Services popup callbacks, so Authorized
redirect URIs are not needed for the standard local login path.

Use dev for:

- Code and UI changes.
- Scraper/indexing experiments.
- Database refreshes and backup updates.
- Performance checks.
- Browser smoke tests.

If `db/animego.sqlite` or `db/backups/current/animego.sqlite` changes, treat
that as local mutable catalog state and verify the intended database snapshot.
These files are ignored by git and should not be committed.

## Production

Production is an isolated localhost installation outside the git repository.
It is managed by scripts in:

```bash
/Users/andrey/.local/share/anime-local-prod/bin/
```

Useful read-only/status commands:

```bash
/Users/andrey/.local/share/anime-local-prod/bin/status
/Users/andrey/.local/share/anime-local-prod/bin/logs
```

Release command, only after the user explicitly asks to release:

```bash
/Users/andrey/.local/share/anime-local-prod/bin/release --ref HEAD
```

Code releases are immutable snapshots created from committed git refs with
`git archive`. Dirty working-tree changes are not included. The release script
refuses dirty worktrees by default; `--allow-dirty` must be explicit and should
only be used when the user accepts that uncommitted work is ignored.

The SQLite catalog and local backup snapshots are not part of git archives.
Treat production database refreshes as a separate explicit operation from code
release. Do not assume that `release --ref HEAD` carries `db/animego.sqlite` or
`db/backups/current/animego.sqlite` into production.

## Normal Workflow

1. Make changes and run all experiments on `http://127.0.0.1:8765/`.
2. Verify the app on dev, including browser checks for UI/player changes.
3. Commit the intended code and docs only; keep database snapshots under ignored
   `db/`.
4. Wait for an explicit user release command.
5. Run the prod release script from the committed ref.
6. Smoke-check prod after release, then stop making changes there.

Never use prod as scratch space. If a change needs investigation, reproduce it
on dev first.
