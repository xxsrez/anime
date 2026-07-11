# AnimeGo User Scanner

The user scanner lets an authenticated Anime Catalog user check AnimeGo through
their own Chrome connection. The server chooses the work, the extension reads
AnimeGo player endpoints, and the server validates and applies only additive
episode/provider changes. A successful scan benefits every catalog user.

This is a catch-up path for the cloud-egress block. It complements, rather than
replaces, the trusted AnimeGo push worker.

## Scan Modes

| Mode | UI | Server selection |
| --- | --- | --- |
| Partial | Click `Scan`, or choose `Partial Scan` from the arrow menu. | Up to 15 current AnimeGo candidates: the selected title when applicable, up to 6 titles from the user's favorites/watching/recent activity, up to 5 least-recently checked titles, and up to 3 random titles. Remaining room is filled from stale candidates. Per-title cooldowns can make the result smaller. |
| Full | Choose `Full Scan` from the arrow menu and confirm. | Every current AnimeGo candidate, ignoring partial-scan cooldowns. The candidate set is ongoing titles plus titles whose known episodes lack playable coverage; it is not every historical row in the database. |

Full Scan creates substantially more AnimeGo requests. Prefer Partial Scan for
routine use and Full Scan for an intentional broad catch-up.

Except for an eligible title currently open in the UI, Partial Scan honors
`animego_title_scan_state.next_eligible_at`. A changed ongoing title becomes
eligible again after 6 hours; a changed non-ongoing title after 24 hours. An
unchanged ongoing title backs off from 18 to at most 54 hours after repeated
no-change checks, an unchanged non-ongoing title waits 7 days, and a title-level
error retries after 30 minutes. Full Scan ignores these cooldowns.

## One-Time Chrome Setup

1. Sign in to Anime Catalog and open `/scanner-setup`.
2. Download `/api/animego-scanner-extension` and unzip it in a permanent local
   folder.
3. Open `chrome://extensions`, enable **Developer mode**, and choose **Load
   unpacked**.
4. Select the unpacked `animego-scanner` folder.
5. Reload Anime Catalog. Clicking `Scan` should open the scanner tab.

The ZIP and setup page require an authenticated Anime Catalog session. This is
an unpacked extension, so after a scanner code update download the ZIP again,
replace the local files, and click **Reload** for the extension in
`chrome://extensions`.

## Running A Scan

1. Keep Anime Catalog signed in and click `Scan` or choose a mode from its arrow
   menu.
2. The app creates a server job and hands its job-scoped token and task list
   to the installed extension.
3. The extension opens a visible scanner tab. It checks each assigned title,
   posts results title by title, and shows checked titles, added episodes,
   providers, and errors.
4. Use **Pause/Resume** to suspend local requests or **Stop** to finish the job as
   stopped. Closing or pausing the tab does not extend the server deadline.

Only one user scan job may be `running` across the deployment. A second user
gets HTTP `409` and a non-secret summary of the active job. Jobs expire two
hours after creation; the expiry releases the global slot.

AnimeGo requests are sequential, with a randomized delay between requests and
bounded exponential retry backoff for transient failures. Do not add parallel
request concurrency to make Full Scan faster.

The extension stores the current job and checkpoint in `chrome.storage.local`.
Reloading the scanner tab or clicking the extension's toolbar action reopens an
unfinished job. Clicking `Scan` again also reopens it when the active global job
belongs to the current user; another user's job remains busy. **Resume** retries
the current title when it was not checkpointed. An AnimeGo `403`, `429`, or bot
challenge is treated as a block: the scanner stops making requests without
completing the job so it can be retried from the checkpoint. Ordinary per-title
errors are posted to the job and the scan continues.

## Write Semantics

The server, not the extension, is the SQLite writer.

- Existing episode metadata is never overwritten.
- An already-playable provider/translation identity is a no-op. If that exact
  provider row exists with a null `embed_url`, the server fills only its missing
  player fields and preserves its existing titles/metadata.
- A result may add a previously unknown episode with playable providers, or a
  previously unknown provider to an existing episode.
- An existing non-playable placeholder that receives its first playable
  provider counts as one new episode and receives episode attribution.
- Results cannot delete titles, episodes, providers, user state, or catalog
  metadata.
- Reposting a completed title result is idempotent and reports
  `already_processed`.
- Successful additions create normal content-update events and invalidate the
  catalog cache.

## API Contract

| Endpoint | Authentication | Purpose |
| --- | --- | --- |
| `POST /api/animego-scans` | Anime Catalog session cookie | Create a `partial` or `full` job. Example body: `{"mode":"partial","current_anime_id":123}`. Returns HTTP `201` with `job`, job `token`, `tasks`, and `origin`; returns `409` when another job is active. |
| `GET /api/animego-scans/<job_id>` | `Authorization: Bearer <job token>` | Read the job counters/status for extension recovery. |
| `POST /api/animego-scans/<job_id>/results` | `Authorization: Bearer <job token>` | Submit one assigned title. Body contains `anime_id` and either an `episodes` array or an `error`. |
| `POST /api/animego-scans/<job_id>/complete` | `Authorization: Bearer <job token>` | Finish the job. Optional body fields are `errors: [{"anime_id":123,"message":"..."}]` and `stopped: true|false`. Retrying completion for an already completed/stopped job is idempotent. |
| `GET /api/animego-scanner-extension` | Anime Catalog session cookie | Download the current extension ZIP. |

Each task includes `anime_id`, `title`, `known_episode_ids`, and
`selection_reason`. `known_episode_ids` contains only already-playable episodes,
so an existing placeholder without a playable source is intentionally checked
again. An episode result contains the upstream episode fields and one or more
playable providers. Provider fields are `provider_id`,
`provider_title`, `translation_id`, `translation_title`, `embed_host`,
`embed_url`, and `embed_url_redacted`.

A per-title error submitted through `/results` counts that item as checked. An
error mentioned only in the final `/complete` summary marks the item failed but
does not claim it was checked.

The job bearer token is separate from the user's login session. Only its SHA-256
hash is stored in SQLite, and it authorizes only the assigned job. Results are
accepted only while that job is `running`; completion is mutation-free when
retried for a completed/stopped job. The token does not grant an app session or
access to any other job.

## Attribution And Audit

The tracked migration creates these tables:

| Table | Purpose |
| --- | --- |
| `animego_scan_jobs` | Actor snapshot (`user_id`, email, name), mode, lifecycle, totals, and content-update run. |
| `animego_scan_job_items` | Assigned title, selection reason, result status, additions, and per-title error. |
| `animego_title_scan_state` | Last check/change, next eligibility, no-change backoff, last user, and last job. |
| `animego_episode_additions` | Episode attribution with `user_id`, `scan_job_id`, timestamp, payload hash, and optional future reversion marker. |
| `animego_provider_additions` | Provider attribution with `user_id`, `scan_job_id`, provider key, timestamp, payload hash, and optional future reversion marker. |

Resolve an email/name to `users.id` first; email and name in
`animego_scan_jobs` are historical snapshots, while `user_id` is the cleanup
key. This read-only query lists both kinds of additions for one exact user ID:

```sql
select id, email, name
from users
where email = 'person@example.com'
   or name = 'Exact display name';

.parameter init
.parameter set @user_id 42

with actor_jobs as (
    select id
    from animego_scan_jobs
    where user_id = @user_id
),
additions as (
    select
        'episode' as kind,
        a.id as audit_id,
        a.scan_job_id,
        a.anime_id,
        a.episode_id,
        null as provider_key,
        a.added_at,
        a.reverted_at
    from animego_episode_additions a
    join actor_jobs j on j.id = a.scan_job_id

    union all

    select
        'provider',
        a.id,
        a.scan_job_id,
        a.anime_id,
        a.episode_id,
        a.provider_key,
        a.added_at,
        a.reverted_at
    from animego_provider_additions a
    join actor_jobs j on j.id = a.scan_job_id
)
select *
from additions
order by added_at desc, kind, audit_id;
```

There is deliberately no deletion or rollback API yet. The query is an audit
input, not a cleanup command. Any future cleanup must account for episode,
provider, content-update, and user-progress dependencies, run under the shared
database-operation lock, and preserve attribution/reversion history.

## Security Boundary

- The extension has exact host permissions for `https://animego.me`, local dev
  at `http://127.0.0.1:8765`, and the production Anime Catalog origin. It does
  not request `<all_urls>`, browser-history, `cookies`, or video-download
  permissions.
- Only those two app origins may start the extension, and the requested origin
  must match the source tab.
- The server accepts results only for a title assigned to that job. It bounds
  request size/counts and validates IDs, duplicate identities, and field
  lengths.
- Player URLs must be HTTPS on the server-owned player-host allowlist; supplied
  host and redacted URL fields must match the normalized URL.
- The extension reads AnimeGo player metadata only. It does not fetch or
  download video streams.

Do not expose `ANIMEGO_PUSH_TOKEN` or `ANIME_SYNC_TOKEN` to the extension. User
scans need neither secret.

## Trusted Worker Coexistence

Keep `scripts/animego_push_worker.py` installed and configured. The trusted
worker performs its complete-snapshot workflow and advances the
`animego:<mode>:last_success` marker; user scan jobs do neither. User scans are
a faster distributed catch-up path for assigned titles.

Both paths validate on the web service and serialize SQLite writes through the
shared database-operation lock. A result submission that cannot obtain that
lock returns `423`; retry it rather than treating the title as successfully
checked. The global one-active-job rule applies to user scan jobs, not to the
trusted worker, so the operation lock remains the final write boundary.

## Troubleshooting

| Symptom | Meaning/action |
| --- | --- |
| The app says the scanner is required | Install the unpacked extension, confirm it is enabled, then reload the app tab. Also reload the extension after replacing its files. |
| HTTP `401` creating a job or downloading the ZIP | Sign in to Anime Catalog again. |
| HTTP `401` on job status/result | The extension has no valid token for that job; start a fresh scan after the old job finishes/expires. |
| HTTP `409` on create | If it is your job, the app reopens the saved scanner. If it belongs to another user, wait for its owner to finish/stop it or for the two-hour expiry. |
| HTTP `410` | The job expired. Start a new scan. |
| HTTP `423` | Another database operation holds the shared lock. Retry the same pending title result. |
| Scanner halts on `403`, `429`, or CAPTCHA | Stop making requests, wait for AnimeGo access to recover, then resume/reload before the job expires. Do not increase request concurrency. |
| Full Scan has fewer titles than the catalog | It scans current ongoing/missing-playable candidates, not every historical AnimeGo title. |
| The app says there is nothing to check | The server created and immediately completed a zero-task job, so it did not open the extension or hold the global slot. |
| A scan finishes with no additions | This is valid: assigned titles were checked but all playable episodes/providers were already known. |

Schema deployment and production release steps remain in
[`Operations_Runbook.md`](../../instructions/Operations_Runbook.md). Follow its
release order: deploy the exact verified code first, wait for that deployment,
then apply tracked migrations and run production smoke checks.
