# Deploying the Project registry and Notion worker

Runbook for t6-core#74. It says what to run, what to check and how to recover.
**Nothing here has been run on the VPS.** The repository's tests prove the
behavior in one process; they do not prove that your Notion subscription, Tailscale
network or Sixto gateway are configured. Fill in the verification log at the end
as you go.

## Shape

| Process | Where | Listens | Holds |
| --- | --- | --- | --- |
| `python -m t6.server` | VPS | public `T6_PUBLIC_BIND` (`/webhook*` and `/frameio/callback` only), tailnet `T6_TAILNET_BIND` (`/tap`, `/queue/*`, `/registry/*`, `/operator/*`) | the only registry, queue and operator databases, on persistent storage (`/data`) |
| `python -m t6.worker` (queue `main`) | supervised separately, same image | nothing | the Notion token and the gateway secret; reaches the server over the tailnet |
| `python -m t6.worker` (queue `frameio`) | Mac mini, same image, its own container and volume | nothing | the Adobe credential and the Frame.io token file (`T6_FRAMEIO_TOKEN_FILE`); nothing else mounts that volume. For the briefing feed it also needs `T6_API_URL`, the t6 API on Sixto (where the Media locations are), and `T6_REGISTRY_READ_SECRET`, that API's read-only registry grant; both or neither, and not the VPS edge, which has no registry. `T6_SERVER_URL` and `T6_WORKER_SECRET` stay the VPS queue server and the `frameio` grant, with no registry read. Variables: README, [Frame.io](../README.md#frameio) |
| `python -m t6.worker` (queue `transcribe`) | Mac mini or a host that mounts the NAS shares, same image | nothing | `ELEVENLABS_API_KEY` (no other process holds it) and `T6_TRANSCRIBE_SHARES`, the path of a JSON file mapping each NAS share name to its mount point on that host. It exits 2 when that file is unset, unreadable, not an object of strings, or empty, and refuses (`outside_shares`) any folder outside the mapped mount points. It reaches the server through the API only and never opens the database. `T6_TRANSCRIBE_RATE` (USD per audio hour, default `0.4`) is read by `python -m t6.server`, not by the worker. Variables: README, [Transcription](../README.md#transcription) |

One server owns the files; there is no second writer. The public listener answers
404 to everything but `/webhook` paths and `/frameio/callback`, so the registry, queue and operator routes
are never on the internet. Bind the tailnet listener to the Tailscale address, not
`0.0.0.0`.

## Configuration

Server: `T6_FRAMEIO_WEBHOOK_SECRET`, `T6_NOTION_WEBHOOK_TOKEN`, `T6_TAP_SECRET`
(the gateway's `T6CORE_SECRET`), one secret per grant (`T6_WORKER_SECRET`,
`T6_REGISTRY_SECRET`, `T6_REGISTRY_READ_SECRET`), `T6_OPERATOR_IDS` (Telegram user
IDs; the first receives cards), the `*_DB` paths and the two binds. Worker:
`T6_SERVER_URL` (the tailnet address, for queue calls only), `T6_API_URL` (the t6 API, for every registry and operator call; required when `T6_DATABASE` is set), `T6_WORKER_SECRET`, `T6_WORKER_QUEUE=main`,
`T6_NOTION_TOKEN`, `T6_NOTION_PROJECTS_DATA_SOURCE`, `T6_NOTION_JOB_ORDERS_DATA_SOURCE`, `T6_GATEWAY_URL`,
`T6_GATEWAY_SECRET`. Keep secrets in the host's secret store or a root-only env
file, never in the image or this repository. Prefer separate secrets per grant so
one leak is one capability. An unset secret turns its route off (503).

## Order of rollout

1. **Prerequisites** (record each in the log): persistent volume for `/data`;
   Tailscale on the VPS, worker host and gateway host; the public webhook URL
   reaches the VPS; Notion integration with access to the Projects data source;
   the Projects data source has `T6 ID` (text) and `Rotation` (select) properties;
   for JO sync, the Job Orders data source has `T6 ID` and `T6 Notes` (rich text) and is accessible to the integration;
   gateway module `t6core` configured with push delivery to `/tap` on the tailnet
   address; gateway change for t6-sixto-gateway#72 deployed if you want code and
   rotation workflows (the cards in #73 work without it).
2. **Start the server** with registry only, then the worker. `t6 health` exits 0.
3. **Notion subscription.** Create the webhook in Notion pointing at
   `/webhook/notion`; Notion sends a `verification_token` once, which the server
   hands to `on_notion_verification`. Store it as `T6_NOTION_WEBHOOK_TOKEN` and
   restart. Subscribe to `page.created`, `page.properties_updated`, `page.deleted`
   and `page.undeleted`. Then change, delete and restore one test page and check
   the registry (`GET /registry/pages/{id}`): the binding turns unavailable on
   delete and available on restore, and its code stays reserved throughout.
4. **Back up** (`t6 admin backup --into DIR`), then **migrate**: `t6 admin registry migrate`
   without `--apply` to read the preview, review it, then with `--apply --preview`.
   All imported Projects are unclassified and block their codes.
5. **Classify** with `t6 admin registry classify` or the Sixto cards. Back up again.
6. Only now allow code and rotation writes by the callers you intend. Run
   `t6 admin schema-check --registry-db ...` and confirm known callers and saved
   references still resolve (they go through the registry's bindings, including
   legacy ones).
7. **End-to-end exercise** (record the result): A in with code X, A out, B takes X,
   A's return is refused as `code_held`, lookup of X gives B, A's saved references
   still give A, the card or response shows the registry change at once and Notion
   pending, then updated.

## Health

`t6 health` (reads only) prints counts and exits 1 on a problem:

- `sync_lag_seconds`: age of the oldest Notion sync not yet confirmed. Alert when it
  keeps growing; the worker is down or Notion is unreachable.
- `sync_persistent_failures`: syncs that used all attempts. The changes stand in the
  registry; Notion shows old values. Fix the cause (token, property names, page access)
  and trigger `POST /registry/pages/{id}/reconcile`.
- `failed_jobs`: queue jobs that failed after their attempts; read their `result`.
- `pending_actions`: operator cards waiting for a tap.
- `waiting_jobs`: jobs that are due and that nothing has taken for more than 15 minutes
  (`WAITING_LIMIT_SECONDS` in `src/t6/ops.py`), by kind with the count and the oldest age in
  seconds, for the queue file (`queue`) and for the internal job table (`internal`). Typical
  cause: no running worker handles that kind. A job whose time is still in the future, or that a
  worker holds under a live lease, is not waiting. This is a problem now, not history:
  `--ignore-history` does not hide it.

Failed jobs and syncs are also listed under `history`. Two flags narrow the check for
deploy tools and monitors:

- `--local`: check this host's databases only and do not ask the t6 API. Without it,
  `t6 health` also needs `T6_API_URL` and `T6_API_SECRET` and fails when the API is
  unreachable.
- `--ignore-history`: list failed jobs and syncs under `history` without failing the
  check. Use it to ask "is this process healthy" after a deploy.

With `T6_SERVER_MODE=edge` (the VPS server) no registry database is required. For a
deploy check: `t6 health --local --ignore-history`.

## Backup and restore

```sh
t6 admin backup --into /backups/2026-11-10          # safe while the server runs
# disaster: stop the server and worker, start from empty /data
t6 admin restore --from /backups/2026-11-10         # verifies checksums and counts, never overwrites
python -m t6.server                           # start; unconfirmed syncs and unsent cards are queued again
```

The backup holds the registry (Projects, bindings, reservations, audit, sync
intents), the queue and the operator actions, each as a consistent snapshot. They
are taken moments apart, so on restore every sync Notion has not confirmed is
marked for dispatch again and every card Telegram never confirmed is queued for
sending again. Both are safe to repeat.

Limits to know: work accepted after the last backup is lost, and the registry is the
authority, so re-run anything an operator did since then. A card edit (showing an
outcome) that was queued but not sent before the failure is not replayed; the
registry change itself is, and `t6 health` plus the Project's `sync` field show the
truth. Keep backups off the VPS.

## Restart exercise

With a sync pending and a card unanswered: restart the server, then the worker.
Expected: the sync reaches Notion once, the card is still answerable, tapping it
twice changes the registry once, and no Project is duplicated. The repository's
tests cover this in one process (`tests/test_operator.py`, `tests/test_ops.py`);
repeat it on the VPS and record it.

## Verification log

| Check | Verified by tests in this repository | Run on the VPS |
| --- | --- | --- |
| Only `/webhook*` on the public listener | yes | not yet |
| Registry and operator routes need explicit grants | yes | not yet |
| Notion change, delete and restore events; code stays reserved | yes (fake Notion) | not yet |
| Migration preview, classification, reused-code writes | yes | not yet |
| A-out, B-reuse, A-return refused, lookup, references | yes | not yet |
| Restart with pending sync and card | yes | not yet |
| Backup and restore with semantic counts | yes | not yet |
| Real Notion webhook verification and subscription | no | not yet |
| Sixto cards in Telegram through the gateway | no | not yet |

### Edge webhook intake

Set `T6_SERVER_MODE=edge` and `T6_ROUTES_FILE` to a YAML file like
[edge-routes.example.yaml](edge-routes.example.yaml), and set every named
secret environment variable before starting `python -m t6.server` (install the
`server` extra). The server validates the file before opening its databases or
listeners. Configure each provider subscription to send only the events meant
for its route; the route selects the job kind and queue, not the payload's type.

The public listener serves configured paths, `GET /health`, and the existing
Frame.io OAuth callback. The tailnet listener serves `GET /health` and the
authenticated `/queue/*` routes. Configure the existing worker secrets for the
queues workers need to claim. The callback still requires a Frame.io worker
claim grant. Edge mode does not open the registry or operator databases.

The Frame.io file route (`job_kind: frameio.file_versioned`) must use `queue: main`, not `frameio`: the
job opens the t6 database, which only the API and the main worker do, and the Frame.io worker never does.
Subscribe it to `file.versioned` and `file.upload.completed`; the job carries the raw webhook body. The main
worker with `T6_DATABASE` set records one Version for a file in a Deliverable's `frameio-folder` Media location
(role review), once per job dedupe key. A file inside a subfolder of that folder is not matched. The main worker
finds the file's folder (and version stack) through the Frame.io worker's read API, route
`GET /frameio/files/<id>/location` (same bearer secret as the other read routes; it answers `folder_id` and
`stack_id`, the folder being the version stack's own parent; 404 for a file Frame.io does not know). Set both
variables in the main worker's environment:

- `T6_FRAMEIO_READ_URL`: the Frame.io worker's read API URL (the Frame.io worker serves it with
  `T6_FRAMEIO_READ_BIND` and `T6_FRAMEIO_READ_SECRET`).
- `T6_FRAMEIO_READ_SECRET`: its bearer secret.

Review activity on a Frame.io project that no show holds (#272) waits instead of going to the default review chat.
The main worker with `T6_DATABASE` set keeps each `frameio.review_done` job in the `frameio_held_activity` table
(migration 41; the Frame.io worker never opens the t6 database) and asks the API (`POST /operator/offers`, kind
`frameio_link`) for one card per Frame.io project. The card goes to the first operator's chat like the other operator
offers; a person with `permission:coordinator` (an operator has it) picks the show, which runs `frameio project link`
as that Person (an active staff Person). The API releases each waiting row once, as a new `frameio.review_done` job
in the internal job table under the key `held:<row>`, when its project is linked by the card, by the verb, or by any
other way: it also checks at start. Without `T6_DATABASE` the main worker holds nothing and keeps the old behaviour.
The `/project` card's Link Frame.io project button needs the API to read the Frame.io worker's `GET /frameio/projects`:
set `T6_FRAMEIO_READ_URL` and `T6_FRAMEIO_READ_SECRET` in the API's environment too. The list is of the T6 account only,
the one the Frame.io worker is configured for (`T6_FRAMEIO_ACCOUNT_ID`; with a sign-in that sees more than one account
and no `T6_FRAMEIO_ACCOUNT_ID` the read fails and the card says Frame.io did not answer). Without the two variables the
button is not offered and only operators use `/project`.

With either variable unset, the main worker does not register `frameio.file_versioned`, so it claims no such
job, and its start-up log says once that Versions from Frame.io events are off and names both variables. A 404
for the file ends the job as ignored; any other read failure fails the job and it is retried.

Frame.io and Notion jobs contain the verified JSON object. Google jobs contain
`headers` with the `x-goog-*` notification headers except the channel token.
Jobs are deduplicated by route and provider event ID (or body hash when absent);
Google uses channel ID and message number. The Notion verification-token
handshake is acknowledged without queuing, as in full mode. The server prints the
token to standard error for the operator to store. For initial edge setup, set
`T6_NOTION_BOOTSTRAP=1` to allow an unset Notion route secret. That route serves only the unsigned Notion verification handshake and logs its bootstrap status at startup; ordinary events are rejected. All other unset route secrets still prevent startup. Store the token, unset bootstrap, and restart.
Unsigned events remain rejected. Full mode remains the default.

### The main worker with the API on Sixto (full mode API, edge VPS)

The VPS server runs in edge mode: it serves `/health` and `/queue/*` and answers 404 to the rest, so it has no registry. The registry is in the t6 database behind the t6 API on Sixto. The main worker therefore has two addresses. `T6_SERVER_URL` is the VPS queue server and carries queue calls only (claim, extend, ack, fail, and the `frameio` queue asks). `T6_API_URL` is the t6 API (on Sixto the API and the main worker share a network namespace, so this is the API on loopback) and carries every `/registry/*` and `/operator/*` call: the Project, Job Order and Deliverable projections, the review Version and review chat links, and operator cards. With `T6_DATABASE` set and no `T6_API_URL`, the main worker refuses to start with a `ConfigError` that names `T6_API_URL`; a registry call never falls back to the queue server. Without `T6_DATABASE` (one server that is both queue and API) it keeps using `T6_SERVER_URL` for registry calls, but it polls only that server's queue file. A full-mode server puts the jobs it starts itself (the four sync kinds, operator cards and taps, `project start` plans, calendar and sharing jobs) in the internal job table in the t6 database, so those jobs wait until a worker that reads the job table (`T6_DATABASE` set) runs; a worker without `T6_DATABASE` never claims them. A worker also claims only the job kinds it has a handler for: a job it cannot run, such as `frameio.file_versioned` without `T6_FRAMEIO_READ_URL` and `T6_FRAMEIO_READ_SECRET`, stays queued with no try used until a worker that can run it starts.

The `main` jobs the API starts itself go to the internal job table in the t6 database (ADR 0006), not to `T6_QUEUE_DB`: `registry.sync`, `deliverable.sync`, `jo.sync`, `project.status_sync`, `operator.deliver`, the `/tap` job and the reconcile `notion.page_changed`. Outside events (Notion and Frame.io webhooks) keep using the VPS queue. When the API starts it moves the waiting `main` jobs of the four sync kinds from its queue file into the internal job table (same kind, same dedupe key; running it twice moves nothing twice) and logs, by kind, the other waiting jobs it leaves in the queue file (`t6.server: waiting jobs left in the queue file: ...`). Read that count before deciding what to do with them.

### Project Start plans (main worker and API only)

`project start` queues a `project_start.preview` internal job on the `main` queue. The main worker with `T6_DATABASE` set builds the plan and answers with the Preview and its token; the caller reads it with `t6 project start <project> --job <id>`. The worker needs `T6_PREVIEW_SIGNING_KEY` (the same key the API signs with, so the API accepts the token at confirm) and `T6_NOTION_TOKEN` (it reads the Project Type to choose the milestone template). Without the signing key the plan jobs stay queued and the worker's start-up log says `project start` plans are off. The plan lists the steps a person does by hand (NAS folders, the Resolve project, the Resolve timelines with their stamps); no worker writes to the NAS or to Resolve. The plan also reads the Project's name from Notion (`ProjectsRow.name`) for the Frame.io project.

#### Project Start and Frame.io (netops note; no live setting is changed by the code)

Confirm only requests the Approval and stores the Start as waiting: it does not ask Frame.io. When the Approval is granted (`approval grant`, in the API) a `project_start.apply` internal job on the `main` queue applies the Start, and the main worker asks the Frame.io worker for the Frame.io project and one folder per Deliverable (request `frameio.create_project_folders`, a write) through the `frameio` queue, with the same grant it uses for review asks (`T6_WORKER_SECRET` may submit to `frameio`); no new setting is needed. The main worker needs `T6_PREVIEW_SIGNING_KEY` for it, as for plans. A failed Start is applied again by confirming it again with the same key and token; that retry runs in the API, which asks the Frame.io worker itself. The API holds no Frame.io token and the Frame.io worker still never opens the t6 database: the apply records the answer as Media locations. Set these on the API (`t6.server`), which asks Frame.io for the retry:

- `T6_FRAMEIO_QUEUE_URL` and `T6_FRAMEIO_QUEUE_SECRET`: the queue server the Frame.io worker polls (the VPS) and a queue grant whose `submit` includes `frameio`. Without them the API uses its own queue file, which only works when the Frame.io worker polls that same queue. Without a reachable Frame.io worker the apply (or the retry) fails with rule `frameio` after `T6_FRAMEIO_WRITE_TIMEOUT_SECONDS` (default 60; the main worker always waits 60) and t6 is unchanged apart from the failure record; a retry is safe.

Set these on the Frame.io worker:

- `T6_FRAMEIO_ACCOUNT_ID`: the T6 account (as for reads). A sign-in that sees more than one account needs it.
- `T6_FRAMEIO_WORKSPACE_ID`: the workspace a new project goes in. Needed only when the account has more than one workspace; with one, the worker uses it.

The Frame.io sign-in (Adobe OAuth Web App credential) needs write scopes. Today it reads only. Creating projects and folders needs the Frame.io V4 project and asset write scopes (create projects, create folders), in addition to the read scopes of the share, comment, file and account reads. Check the scope names against the Adobe Developer Console and Frame.io V4 reference when you change the credential; the routes below are a reconstruction of the V4 API and were not called live. No scope for creating shares is needed, and none is used. After a scope change the Frame.io worker must sign in again (the old token keeps the old scopes).

The routes used (reconstructed, no live call): `GET /v4/accounts/{account}/workspaces`, `POST /v4/accounts/{account}/workspaces/{workspace}/projects` (body `{"data": {"name": ...}}`), `GET /v4/accounts/{account}/projects/{project}`, `GET /v4/accounts/{account}/folders/{folder}/children`, `POST /v4/accounts/{account}/folders/{folder}/folders`.

### Booking calendars (main worker and API only)

Every Booking change queues a `booking.calendar_sync` internal job. The main worker with `T6_DATABASE` set handles it (ADR 0003: only the API and the main worker read the Google settings; apps and the gateway never do).

- `T6_GOOGLE_BAY_CALENDARS`: a JSON object of Bay ID (`t6b_...`) to Google calendar ID, for example `{"t6b_example": "example@group.calendar.google.com"}`. A Bay missing from it fails its jobs with "Bay has no calendar".
- `GOOGLE_APPLICATION_CREDENTIALS`: the path of the Google service account key file (JSON, mode 600, in the root-only env directory on Sixto, never in the image or the repository). One service account; each Bay calendar stays owned by the personal Gmail account and is shared to the service account's email with "Make changes and manage sharing" (issue #186). Fallback if Google refuses that: an OAuth user credential.
- Install the `google` extra (`pip install 't6[google]'`), which adds `cryptography` to sign the service account sign-in. Nothing else is added: HTTP is the standard library.
- Event content: the title is the Project's name, the description is `Person: <name>` and `Booking ID: <t6g_ id>`, nothing else. The names come from Notion, so `T6_NOTION_TOKEN` is required with the calendars; the worker refuses to start without it (`ConfigError`).
- With either variable unset, the job finishes with the result `no calendar configured` (it does not fail, retry or die). The Booking is kept; Bookings changed while no calendar was set up are not written later. To give the Bookings that already exist their events when the calendar goes live, run `t6 admin calendar resync` on the database host (`--registry-db FILE`, default `T6_REGISTRY_DB`). It queues one `booking.calendar_sync` job for every Booking that is not cancelled and has not ended, through the same function a Booking change uses, and prints `{"queued": N}`. A Booking that already has a sync queued or claimed is skipped, so a second run does not double it. It is an admin command, not a verb.
- A job that failed five times is dead. `t6 health` lists dead internal jobs by kind (`dead_internal_jobs`) and counts them as a problem; `--ignore-history` reports them without failing.
- A calendar write is safe to repeat: the event ID is derived from the Booking ID, Google's 409 (event exists) turns into an update, and a 404 or 410 on delete counts as already gone.

#### Edits made in Google Calendar (inbound, main worker)

A person who changes a Booking event in Google Calendar changes the Booking, through the same rules as the verbs.

- Edge: add the `/events/google/calendar` route from [edge-routes.example.yaml](edge-routes.example.yaml) (verifier `google_channel`, job kind `google.calendar_changed`, queue `main`). The edge checks `X-Goog-Channel-Token` on every call and queues the `x-goog-*` headers without the token; the body is not used.
- Main worker (with the calendar settings above): `T6_GOOGLE_NOTIFY_URL` is the public HTTPS address of that route (Google needs a valid certificate), and `T6_GOOGLE_CHANNEL_TOKEN` is the shared channel token (at most 256 characters; use the same variable name and value in the edge environment, which the route's `secret_env` names; never print it). With either unset, both jobs finish with `no inbound calendar edits configured`.
- A daily internal job `google.renew_channels` keeps one notification channel per Bay calendar. It asks Google for a 7-day channel and replaces it when less than 3 days remain, or when the token changed; the old channel is stopped. The worker queues the first run at start-up, and each run queues the next, so a worker restart restores the chain after a dead run. The run's key is the day and a fingerprint of the token, so a worker that starts with a changed `T6_GOOGLE_CHANNEL_TOKEN` queues a run at once (the new channels carry the new token); a restart with the same token queues nothing more that day. The table `calendar_channels` holds channel ID, resource ID, expiry and a SHA-256 fingerprint of the token (not the token). The same run is the daily catch-up: it also lists every Bay calendar from its stored sync token and applies what it finds, as a notification would, so a lost notification is caught within a day (fact check #186, section 3; a calendar that cannot be read fails the run after the others were caught up, and the job retries). Check `t6 health` for dead `google.renew_channels` jobs.
- Sharing follows the `bookings` permission (#271). `person grant` or `person revoke` of `bookings` queues a `booking.calendar_sharing` internal job in the same transaction. The main worker (needs `T6_NOTION_TOKEN`, as the Person's Google email is on their Notion Contact) sets `writer` for holders and `reader` for other active staff on every Bay calendar, without notification email. Revoke sets `reader`; it never deletes an entry. A Person with no email on the Contact is skipped and counted (`no_email` in the job result). `person classify` and `person rebind` also queue this job (the kind decides who may edit; the bound Notion page holds the email). No person verb deactivates a Person yet; a Person who is inactive loses edit at the next daily run.
- A daily internal job `google.reconcile_sharing` (queued at worker start; each run queues the next, like `google.renew_channels`) compares each Bay calendar's ACL with t6 and corrects drift (t6 wins). It reads emails only for active staff, and one failed lookup is counted (`lookup_failed`) and skipped, not fatal; in a run with a failed lookup no unknown writer is lowered, because it may be the Person that could not be read. It owns entries whose email is a Person's email, and it lowers to `reader` any `writer` whose email is no Person's (for example someone added by hand in Google) and any `writer` entry that is a group, a domain or the public (`default`) rule, because calendar sharing is the permission check for inbound edits. It never changes an entry with role `owner` (the calendar owner and the service account are both managers) or the service account's email in any role; other entries whose email is no Person's are left and counted as `unmanaged`. Each fix is logged as `calendar sharing fix: calendar=<hash> identity=<hash> role <from>-><to>` (logger `t6.google.sharing`; short SHA-256 hashes, no emails) and listed in the job result. Check `t6 health` for dead `booking.calendar_sharing` and `google.reconcile_sharing` jobs. With no calendar configured both jobs finish `skipped`.
- On a notification the worker lists the calendar's changes since the stored sync token (`calendar_sync_tokens`; a first list sets the baseline, a 410 falls back to a full list) and matches each event to its Booking by the event ID. An event t6 did not write is left alone.
- Calendar sharing is the permission check for inbound edits: only a `writer` can edit a Bay calendar in Google, and the reconcile job of #271 (`google.reconcile_sharing`) is what keeps `writer` equal to the `bookings` holders (it also lowers a hand-added writer who is no Person). If that job is dead or the calendar is shared by hand, the check is not true; watch `t6 health`.
- Google names no editor (Events resource, `docs/research/google-calendar-fact-check.md` section 4), so with the real adapter every edit has no known editor. It is accepted as made by a `bookings` holder: the actor is the internal service actor `google-calendar` (not a Person; no API secret can use it; it may run only `booking move`, `booking extend` and `booking cancel`), and the Activity entry says the change was made in Google Calendar and that Google does not name the editor. No Person is guessed. `booking move`, `booking extend` or `booking cancel` runs through the same rules as the verbs (event deleted: cancel; start or Bay changed or end earlier: move; later end: extend).
- When an editor is known (the fake, or a future source), the stricter rule holds: the editor must be a Person with the `bookings` permission whose Notion Contact email equals the editor's email (case-insensitive), and that Person is the actor; anyone else is undone with a note. The job needs `T6_NOTION_TOKEN` to read the emails.
- A clash (overlap), a named editor without the permission, or an event without a timed range is undone: the Booking stays as it was and the event is written back with `Note: change reverted: <reason>.` added to its description. A Booking change in progress (its `booking.calendar_sync` job is queued) makes the notification stale; it is skipped.
- No loop: an event whose start, end and Bay already equal the Booking is our own write coming back, and nothing runs. Each event version (etag, else updated time) is handled once (`calendar_event_versions`).
- Known limit: the Google Events resource names no last editor, so the audit trail says "Google Calendar", not who. Open owner question: accept this, or make the calendars view-only and revert every edit made in Google Calendar.

Workers can also poll internal jobs by setting `T6_DATABASE` to the local t6 SQLite database path. The worker polls that job table and its configured VPS queue in turn; the database must be shared with internal job submitters. Back up the database before upgrading. The main worker also requires `T6_GATEWAY_URL` and `T6_GATEWAY_SECRET` for linked-chat subscriptions and pulls; the secret must authenticate the `t6core` module. It reconciles subscriptions on startup and each worker poll, and retries gateway failures on a later poll. It stores message IDs and flags, and member IDs and presence, but no message content. The main worker with `T6_DATABASE` also handles `frameio.share_viewed`: route the Frame.io `share.viewed` webhook (`/webhook/frameio/shares`) to the `main` queue. The job carries the webhook body; a view of a share recorded on a Delivery writes one Activity entry per event, and any other share is dropped. The Frame.io worker never opens the t6 database.
