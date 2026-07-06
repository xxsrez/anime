# Local Environments And Release Rules

This project uses two fixed localhost environments. Keep the split strict so
development work cannot break the stable local production site.

## Environment Map

| Environment | URL | Files | Database | Agent rule |
| --- | --- | --- | --- | --- |
| dev/test/scratch | `http://127.0.0.1:8765/` | `/Users/andrey/Documents/Anime` | `/Users/andrey/Documents/Anime/data/animego.sqlite` | Safe for regular edits, scraping, indexing, restarts, and browser testing. |
| prod | `http://127.0.0.1:8766/` | `/Users/andrey/.local/share/anime-local-prod` | `/Users/andrey/.local/share/anime-local-prod/shared/animego.sqlite` | Release-only. Do not edit, restart, scrape, index, or test against it unless the user explicitly asks for a prod operation. |

Port `8776` is retired for this project. Do not use it for Anime dev/prod work
unless the user explicitly changes the scheme again.

## Development

Run and test the current working tree on dev:

```bash
python3 server.py --host 127.0.0.1 --port 8765
```

Use dev for:

- Code and UI changes.
- Scraper/indexing experiments.
- Database refreshes and backup updates.
- Performance checks.
- Browser smoke tests.

If `data/animego.sqlite` or `backups/current/animego.sqlite` changes, treat
that as local mutable catalog state and verify the intended database snapshot
before committing or releasing it.

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

Releases are immutable snapshots created from committed git refs with
`git archive`. Dirty working-tree changes are not included. The release script
refuses dirty worktrees by default; `--allow-dirty` must be explicit and should
only be used when the user accepts that uncommitted work is ignored.

During release, the production catalog database is refreshed from the released
backup database while `user_title_state` is preserved when referenced titles
still exist.

## Normal Workflow

1. Make changes and run all experiments on `http://127.0.0.1:8765/`.
2. Verify the app on dev, including browser checks for UI/player changes.
3. Commit the intended code, docs, and backup/database snapshot changes.
4. Wait for an explicit user release command.
5. Run the prod release script from the committed ref.
6. Smoke-check prod after release, then stop making changes there.

Never use prod as scratch space. If a change needs investigation, reproduce it
on dev first.
