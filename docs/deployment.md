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
| `python -m t6.worker` (queue `frameio`) | Mac mini, same image, its own container and volume | nothing | the Adobe credential and the Frame.io token file (`T6_FRAMEIO_TOKEN_FILE`); nothing else mounts that volume. Variables: README, [Frame.io](../README.md#frameio) |

One server owns the files; there is no second writer. The public listener answers
404 to everything but `/webhook` paths and `/frameio/callback`, so the registry, queue and operator routes
are never on the internet. Bind the tailnet listener to the Tailscale address, not
`0.0.0.0`.

## Configuration

Server: `T6_FRAMEIO_WEBHOOK_SECRET`, `T6_NOTION_WEBHOOK_TOKEN`, `T6_TAP_SECRET`
(the gateway's `T6CORE_SECRET`), one secret per grant (`T6_WORKER_SECRET`,
`T6_REGISTRY_SECRET`, `T6_REGISTRY_READ_SECRET`), `T6_OPERATOR_IDS` (Telegram user
IDs; the first receives cards), the `*_DB` paths and the two binds. Worker:
`T6_SERVER_URL` (the tailnet address), `T6_WORKER_SECRET`, `T6_WORKER_QUEUE=main`,
`T6_NOTION_TOKEN`, `T6_NOTION_PROJECTS_DATA_SOURCE`, `T6_GATEWAY_URL`,
`T6_GATEWAY_SECRET`. Keep secrets in the host's secret store or a root-only env
file, never in the image or this repository. Prefer separate secrets per grant so
one leak is one capability. An unset secret turns its route off (503).

## Order of rollout

1. **Prerequisites** (record each in the log): persistent volume for `/data`;
   Tailscale on the VPS, worker host and gateway host; the public webhook URL
   reaches the VPS; Notion integration with access to the Projects data source;
   the Projects data source has `T6 ID` (text) and `Rotation` (select) properties;
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
`T6_NOTION_BOOTSTRAP=1` to allow a missing Notion route secret; other routes
still require their secrets. Store the token, unset bootstrap, and restart.
Unsigned events remain rejected. Full mode remains the default.

Workers can also poll internal jobs by setting `T6_DATABASE` to the local t6 SQLite database path. The worker polls that job table and its configured VPS queue in turn; the database must be shared with internal job submitters. Back up the database before upgrading.
