# Railway configuration

`railway.ts` manages the `web` service and its existing `web-volume` mount as a
named partial. Environment variables and the separate `daily-sync` service are
managed outside this file. Railway evaluates this file when `config plan` or
`config apply` runs; deployments do not apply it automatically.

Install the pinned TypeScript SDK with Node.js 22 or newer:

```sh
npm ci --prefix .railway --ignore-scripts
```

Before planning or applying, verify that the Railway CLI is linked to the
intended project and environment. Review the entire plan, especially volume
attachments, service sources, and variables. Do not apply unexpected removals.
Follow `docs/instructions/Operations_Runbook.md` for production changes and
release verification.
