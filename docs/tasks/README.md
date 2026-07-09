# Tasks

This directory is reserved for complex investigations that need durable history.

Create `tasks/{task-name}/` only for work that is expected to span more than a
short session, needs multiple approaches, or requires a clear investigation
diary. Use lowercase hyphen-separated names.

Recommended task layout:

```text
tasks/{task-name}/
├── README.md
├── changelog.md
├── task-description.md
├── implementation-plan.md
├── sources/
├── experiments/
└── archive/
```

`changelog.md` should be append-only with the newest entry at the top.

## Current Tasks

- [`project-hardening/`](project-hardening/) - repository-wide reliability,
  concurrency, recovery, security, performance, and reproducibility pass.
