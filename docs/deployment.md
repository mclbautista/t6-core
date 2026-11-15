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

Frame.io and Notion jobs contain the verified JSON object. Google jobs contain
`headers` with the `x-goog-*` notification headers except the channel token.
Jobs are deduplicated by route and provider event ID (or body hash when absent);
Google uses channel ID and message number. The Notion verification-token
handshake is acknowledged without queuing, as in full mode. The server prints the
token to standard error for the operator to store. For initial edge setup, set
`T6_NOTION_BOOTSTRAP=1` to allow an unset Notion route secret. That route serves only the unsigned Notion verification handshake and logs its bootstrap status at startup; ordinary events are rejected. All other unset route secrets still prevent startup. Store the token, unset bootstrap, and restart.
Unsigned events remain rejected. Full mode remains the default.

### The main worker with the API on Sixto (full mode API, edge VPS)

The VPS server runs in edge mode: it serves `/health` and `/queue/*` and answers 404 to the rest, so it has no registry. The registry is in the t6 database behind the t6 API on Sixto. The main worker therefore has two addresses. `T6_SERVER_URL` is the VPS queue server and carries queue calls only (claim, extend, ack, fail, and the `frameio` queue asks). `T6_API_URL` is the t6 API (on Sixto the API and the main worker share a network namespace, so this is the API on loopback) and carries every `/registry/*` and `/operator/*` call: the Project, Job Order and Deliverable projections, the review Version and review chat links, and operator cards. With `T6_DATABASE` set and no `T6_API_URL`, the main worker refuses to start with a `ConfigError` that names `T6_API_URL`; a registry call never falls back to the queue server. Without `T6_DATABASE` (one server that is both queue and API) it keeps using `T6_SERVER_URL`.

The `main` jobs the API starts itself go to the internal job table in the t6 database (ADR 0006), not to `T6_QUEUE_DB`: `registry.sync`, `deliverable.sync`, `jo.sync`, `project.status_sync`, `operator.deliver`, the `/tap` job and the reconcile `notion.page_changed`. Outside events (Notion and Frame.io webhooks) keep using the VPS queue. When the API starts it moves the waiting `main` jobs of the four sync kinds from its queue file into the internal job table (same kind, same dedupe key; running it twice moves nothing twice) and logs, by kind, the other waiting jobs it leaves in the queue file (`t6.server: waiting jobs left in the queue file: ...`). Read that count before deciding what to do with them.

### Booking calendars (main worker and API only)

Every Booking change queues a `booking.calendar_sync` internal job. The main worker with `T6_DATABASE` set handles it (ADR 0003: only the API and the main worker read the Google settings; apps and the gateway never do).

- `T6_GOOGLE_BAY_CALENDARS`: a JSON object of Bay ID (`t6b_...`) to Google calendar ID, for example `{"t6b_example": "example@group.calendar.google.com"}`. A Bay missing from it fails its jobs with "Bay has no calendar".
- `GOOGLE_APPLICATION_CREDENTIALS`: the path of the Google service account key file (JSON, mode 600, in the root-only env directory on Sixto, never in the image or the repository). One service account; each Bay calendar stays owned by the personal Gmail account and is shared to the service account's email with "Make changes and manage sharing" (issue #186). Fallback if Google refuses that: an OAuth user credential.
- Install the `google` extra (`pip install 't6[google]'`), which adds `cryptography` to sign the service account sign-in. Nothing else is added: HTTP is the standard library.
- Event content: the title is the Project's name, the description is `Person: <name>` and `Booking ID: <t6g_ id>`, nothing else. The names come from Notion, so `T6_NOTION_TOKEN` is required with the calendars; the worker refuses to start without it (`ConfigError`).
- With either variable unset, the job finishes with the result `no calendar configured` (it does not fail, retry or die). The Booking is kept; Bookings changed while no calendar was set up are not written later, so run a re-sync of future Bookings when the calendar goes live.
- A job that failed five times is dead. `t6 health` lists dead internal jobs by kind (`dead_internal_jobs`) and counts them as a problem; `--ignore-history` reports them without failing.
- A calendar write is safe to repeat: the event ID is derived from the Booking ID, Google's 409 (event exists) turns into an update, and a 404 or 410 on delete counts as already gone.

Workers can also poll internal jobs by setting `T6_DATABASE` to the local t6 SQLite database path. The worker polls that job table and its configured VPS queue in turn; the database must be shared with internal job submitters. Back up the database before upgrading. The main worker also requires `T6_GATEWAY_URL` and `T6_GATEWAY_SECRET` for linked-chat subscriptions and pulls; the secret must authenticate the `t6core` module. It reconciles subscriptions on startup and each worker poll, and retries gateway failures on a later poll. It stores message IDs and flags, and member IDs and presence, but no message content.
