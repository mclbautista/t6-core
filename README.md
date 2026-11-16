# t6-core

The shared T6 Python library. This repository was renamed from **t6-notion**
before its first commit. The distribution and import namespace are `t6`.

Requires Python 3.11 or newer. `t6` is not on PyPI: install it from this
repository, pinned to a release tag, with
`pip install 't6 @ git+https://github.com/mclbautista/t6-core.git@v0.6.7'`.
Where this README says `pip install 't6[extra]'`, use the same form with the
extra, for example
`pip install 't6[notion] @ git+https://github.com/mclbautista/t6-core.git@v0.6.7'`.
The base package has no third-party runtime dependencies and includes
`t6.domain`, `t6.lookup`, `t6.drives`, `t6.registry`, and the `t6` command.

Domain terms are defined in [`CONTEXT.md`](CONTEXT.md).

## Domain

`t6.domain` models T6 once, as immutable dataclasses that do no I/O:
Project, Episode, Deliverable, JobOrder, Contact, Studio, Machine (Bay),
Camera and ExternalRef.

- Each entity stored in Notion, except a Project, is identified by its Notion
  page ID. Two instances with the same page ID compare equal. Relations are
  page IDs.
- A Project is identified by `t6_id`, its T6 Project ID from the
  [Project registry](#project-registry), and by its page ID until it is
  registered (`None`). `Project.key` is that identity, and equality is on it:
  two Projects with a T6 ID are equal when the IDs match, whatever page each
  was read from. A Project read before registration and the same Project read
  after are not equal values; compare through `ProjectRefs.same`. Episodes
  carry the Project's `key`. Job Orders, Deliverables and External Links (until its migration) still
  hold page IDs; see [Migrating existing Projects](#migrating-existing-projects).
- A Project's human key is its Project Code, which may be blank (`None`).
- `Project.episodes` derives Episodes 1 to `Eps` for the series Project Types
  (`SERIES_PROJECT_TYPES`). Films and other types have no Episodes.
- An ExternalRef (system, kind, external ID, URL, internal ID) mirrors one
  retired External Links row. System, kind and external ID identify the object; the URL
  and the optional internal ID (the tool's own key, such as the project
  server's project ID) are carried but do not affect equality.

### Events and jobs

Tool events and the work they trigger share one typed shape, used by the
webhook server, the worker and poll-and-diff.

- `FrameioCommentCreated` (Frame.io comment ID, Frame.io project ID, optional
  file ID) and `ProjectStatusChanged` (Project page ID, new Status) are Events.
- Each Event has a `dedupe_key` built from the tool's own IDs. The same tool
  event has the same key whether it came from a webhook or from poll-and-diff;
  `source` and `received_at` are carried but are not part of the key.
- A `Job` carries its `JobKind`, a read-only JSON-ready payload and a dedupe
  key. `Job.for_event(kind, event)` keys the job by kind and event, so one
  event queues each kind of job at most once.

## Webhook server, queue and worker

`t6.server` runs on the VPS: `python -m t6.server`. `t6.worker` runs on the
Mac mini. Both use only the standard library.

- **Two listeners.** The public one serves only `/webhook` and `/webhook/*`
  (Frame.io and Notion) and `GET /frameio/callback` (the
  [Frame.io sign-in](#frameio) redirect). The tailnet one serves `POST /tap` (the gateway),
  `/queue/*` (workers and callers), `POST /review/timers` (the Frame.io worker; an edge server serves this one too, because the timers live in its queue file and `frameio.review_done` is claimed from its `main` queue) and `/registry/*` (the
  [Project registry](#project-registry)). Nothing else is served on the
  public one.
- **Signatures.** Frame.io: `v0:{timestamp}:{body}` HMAC-SHA256, five-minute
  window in both directions. Notion: HMAC-SHA256 of the raw body; the event's
  own timestamp must be less than 25 hours old, because Notion retries for
  about 24 hours. A bad or stale request is answered 401 and queues nothing.
  A secret that is not set turns its route off (503).
- **Queue** (`JobQueue`, SQLite WAL): at-least-once, one job per
  (queue, dedupe key), lease-based claims, named queues (`main`, `frameio`), a
  typed result per job, and quiet-period timers stored in the database. A job
  that loses its lease five times is failed.
- **Events.** A Frame.io `comment.created` becomes a `frameio.resolve_comment`
  job for the Frame.io worker, which starts a quiet timer per (file, author)
  (see [Frame.io](#frameio)); when it fires it becomes a `frameio.review_done`
  job with the comment count and the first and last comment times. A Notion `page.properties_updated`,
  `page.created`, `page.deleted` or `page.undeleted` becomes a
  `notion.page_changed` job (Notion names the changed properties, not the new
  value, so a worker reads the page). A tap becomes a `tap` job keyed by the
  gateway's `Idempotency-Key`.
- **Worker.** `Worker(QueueClient(HttpTransport(url), secret), "main", handlers)`
  claims, runs and acks one job per `run_once()`. The server never calls a
  worker.
- **Worker entrypoint.** `python -m t6.worker` polls one queue in a loop,
  sleeps between empty polls and exits cleanly on SIGTERM. It exits with code 2
  and a message on stderr when a required variable is missing or no handler is
  registered for its queue. The `main` queue gets the
  [Notion sync](#notion-sync-and-reconciliation) handlers when the two Notion
  variables below are set. The isolated `diagnostic` queue has a built-in
  `diagnostic.echo` handler that returns its input payload. It is for test
  payloads only: do not put secrets or real jobs in it. The `frameio` queue
  runs the [Frame.io worker](#frameio) and calls its `tick()` before every
  poll. It needs the Adobe credential and token file variables below. A handler that raises
  `t6.worker.Refused` fails its job for good, with no retry.

| Worker variable | Use |
| --- | --- |
| `T6_SERVER_URL` | required: the server's tailnet address, for example `http://100.64.0.1:8081` |
| `T6_WORKER_SECRET` | required: the queue bearer secret; for `diagnostic`, use the server's `T6_DIAGNOSTIC_SECRET` |
| `T6_WORKER_QUEUE` | required: the queue to poll (`main`, `frameio`, or `diagnostic`) |
| `T6_WORKER_POLL_SECONDS` | optional: sleep after an empty poll, default `5` (the internal job table is looked at every 0.25 s meanwhile) |
| `T6_NOTION_TOKEN` | main worker: Notion integration token, for the Project registry sync |
| `T6_NOTION_PROJECTS_DATA_SOURCE` | main worker: ID of the Projects data source |
| `T6_NOTION_JOB_ORDERS_DATA_SOURCE` | main worker: ID of the Job Orders data source |
| `T6_REVIEW_CHAT` | main worker: required when the gateway variables are set, the Telegram chat ID that gets the [review message](#frameio) |
| `T6_REVIEW_TOPIC` | main worker: optional, the forum topic ID in that chat. Leave it unset for the General topic: Telegram rejects topic 1 |
| `T6_GATEWAY_URL`, `T6_GATEWAY_SECRET` | Frame.io worker: required, the gateway whose `/notify` sends the sign-in alert |
| `T6_FRAMEIO_ALERT_CHAT` | Frame.io worker: required, the Telegram chat ID that gets the sign-in alert |
| `T6_FRAMEIO_ALERT_TOPIC` | Frame.io worker: optional, the forum topic ID in that chat; unset posts to General |
| `T6_FRAMEIO_CLIENT_ID`, `T6_FRAMEIO_CLIENT_SECRET` | Frame.io worker: required, the Adobe OAuth Web App credential. Give them to no other process |
| `T6_FRAMEIO_REDIRECT_URI` | Frame.io worker: required, the `https://` address of the server's `/frameio/callback`, exactly as set on the Adobe credential |
| `T6_FRAMEIO_TOKEN_FILE` | Frame.io worker: required, where the token is kept, on a volume only this worker mounts |
| `T6_FRAMEIO_ACCOUNT_ID` | Frame.io worker: optional, the Frame.io account ID; needed only when the sign-in sees more than one account |
| `T6_API_URL`, `T6_REGISTRY_READ_SECRET` | Frame.io worker: turn on the briefing feed from Media locations. `T6_API_URL` is the t6 API on Sixto, not the queue server; the secret is the API's read-only registry grant. Needed together, used for this client only; the worker's own `T6_WORKER_SECRET` and its `frameio` grant stay without registry read |
| `T6_SERVER_URL`, `T6_WORKER_SECRET` (the secret needs registry read) | main worker: `T6_SERVER_URL` is for queue calls only. With `T6_DATABASE` set it reads Media locations from that database, not over HTTP, and needs `T6_API_URL` (the t6 API) for every registry and operator call, or it refuses to start |
| `T6_FRAMEIO_READ_BIND`, `T6_FRAMEIO_READ_SECRET` | Frame.io worker: optional, turn on the [read API](#frameio): `host:port` on the LAN or tailnet address (never a public one) and the bearer secret callers send. Both are needed together |
| `T6_FRAMEIO_READ_URL`, `T6_FRAMEIO_READ_SECRET` | t6 API: Frame.io worker read API URL and bearer secret, needed for `version add` to resolve the file and stack. The main worker also uses both (with `T6_DATABASE`) to read a file's location for Versions from Frame.io events; without them that handler is off and the start-up log says so. The API also uses both for the `/project` card's Link Frame.io project button (the T6 account's projects, newest first); without them the button is not offered. |

| Variable | Use |
| --- | --- |
| `T6_FRAMEIO_WEBHOOK_SECRET` | Frame.io signing secret |
| `T6_NOTION_WEBHOOK_TOKEN` | Notion `verification_token` |
| `T6_TAP_SECRET` | legacy full-server tap secret; API taps use the gateway identity in `T6_API_SECRETS` |
| `T6_WORKER_SECRET` | main worker: claims `main`, may submit to `main` and `frameio`, reads Projects and reports Notion sync results |
| `T6_FRAMEIO_WORKER_SECRET` | Frame.io worker: claims `frameio`, may start review timers (`POST /review/timers`); also turns on `/frameio/callback` |
| `T6_FRAMEIO_CALLER_SECRET` | a caller outside the main worker (t6-notion-reports): may submit to `frameio` and read its results, nothing else |
| `T6_DIAGNOSTIC_SECRET` | optional isolated diagnostic queue submit/claim credential; must differ from all other server secrets |
| `T6_REGISTRY_SECRET` | registry caller that may register and read Projects |
| `T6_REGISTRY_READ_SECRET` | registry caller that may only read Projects |
| `T6_QUEUE_DB`, `T6_PUBLIC_BIND`, `T6_TAILNET_BIND` | database path, listener addresses |
| `T6_REGISTRY_DB` | Project registry database path, default `t6-registry.sqlite` |
| `T6_CONCERN_AUTOSCAN` | Set to `1` to scan PM Notes for Concerns automatically in the API server. Off by default; needs the LLM service. |
| `T6_CONCERN_OBSERVE_CLOSES` | Set to `1` to let `concern observe` close a Concern its run did not find. Off by default: a run closes nothing. |
| `T6_CONCERN_AUTOSCAN_SECONDS` | Seconds between automatic scans, default `900`, minimum `60` |
| `T6_OPERATOR_IDS` | Telegram user IDs allowed to tap operator cards and send `/project`, comma separated; the first receives the cards core starts, and a `/project` card goes to whoever sent it. Unset turns operator actions off |
| `T6_STANDING_CC` | A Contact page ID, added to the cc of every Job Order (`t6.domain.studio_people.standing_cc`). Unset means none |
| `T6_OPERATOR_DB`, `T6_OPERATOR_TTL_SECONDS` | operator action database, default `t6-operator.sqlite`; how long a card stays live, default 7 days |
| `T6_REVIEW_QUIET_SECONDS` | server: how long one person must make no new comment on one Frame.io file before the review message is sent, default 900 (15 minutes); must be a positive number. Used when the clip length or the comment's position is not known (see `T6_REVIEW_LOOP_ALLOWANCE_SECONDS`) |
| `T6_REVIEW_LOOP_ALLOWANCE_SECONDS` | server: time added to the part of the clip left after a person's last comment, so they can watch it through once more; default 480 (8 minutes); must be a positive number. The wait is `(clip length - the comment's position) + this`, with no cap |
| `T6_FRAMEIO_SHARE_SCAN_LIMIT` | Frame.io worker: how many of a project's newest live share links have their assets read to find the one that holds a file, default 30; a whole number of 1 or more. When the file is in none of them and live shares were left unread, the review message says "Share link: unknown" instead of "no share link" |
| `T6_FRAMEIO_SHARE_CACHE_SECONDS` | Frame.io worker: how long a project's share list and each share's asset list are kept in memory, default 3600 (60 minutes); 0 turns it off. A share created or changed within that time may be missed until its entry expires. A failed read is never kept |
| `T6_GATEWAY_URL`, `T6_GATEWAY_SECRET` | main worker: the Sixto gateway and this module's secret there; turn on delivery of operator cards |

On first open of a t6 database, `T6_OPERATOR_IDS` seeds the `operator` permission for matching Persons; unmatched IDs are reported on stderr. The seed is recorded even when the variable is empty, and later changes to it do not change Person permissions. Set it before the first open of an existing database containing Persons. Legacy operator cards still use the variable independently until their authorization is migrated.

Until the CLI becomes an API client (#200), set `T6_PERSON_ID` to the local caller’s Person ID. Registered verbs use that Person’s stored permissions; it is configuration, not a command-line actor flag. API authentication will supply the actor in #197 and #200.

Tests use `t6.server.testing.FakeClock`; no test opens a connection to a
provider.

## Consumer contract

Every app uses t6-core for Notion, LLMs, privacy, Frame.io, Resolve and the
fleet. The rules are in
[`docs/adr/0002-consumer-contract.md`](docs/adr/0002-consumer-contract.md).

## MCP server

`t6 mcp` runs the read-only MCP server (stdio, over the t6 API); see
[MCP server](docs/deployment.md#mcp-server) for the settings and a client entry.

## Project registry

`t6.registry` gives every Project a permanent T6 Project ID that does not
depend on its Project Code or its Notion page. The registry owns identity,
code, rotation, page bindings and audit history; Notion owns the descriptive
fields. The decision is recorded in
[`docs/adr/0001-project-registry-owns-project-identity.md`](docs/adr/0001-project-registry-owns-project-identity.md).
It needs no extra.

- `ProjectRegistry` is the contract. `SqliteProjectRegistry(path)` is the
  durable implementation (SQLite WAL, standard library). Run one instance for
  all of T6; `python -m t6.server` opens it at `T6_REGISTRY_DB`.
- `register(actor=, idempotency_key=, notion_page_id=None, code=None, rotation=Rotation.OUT)`
  mints a Project with a new ID (`t6p_` and an opaque suffix) at revision 1.
  It returns `Registered`, `PageAlreadyBound`, `CodeHeld` or `KeyReused`.
- A registration never matches an existing Project by code or name. Two
  registrations are two Projects, even with the same code.
- `set_code`, `set_rotation` and `bind_page` change one Project. Each takes
  the actor, an idempotency key and the `expected_revision` the caller last
  read, and returns `Changed`, `NotFound`, `RevisionConflict`, `CodeHeld`,
  `PageAlreadyBound` or `KeyReused`. A change to the value the Project
  already has is accepted and changes nothing.
- Every accepted change raises the revision by one and commits the new state,
  an audit entry (actor, time, action, previous and new values) and a sync
  intent in one transaction. A refused change writes nothing.
  `pending_sync()` lists the intents not yet shown in Notion; see
  [Notion sync and reconciliation](#notion-sync-and-reconciliation).
- A repeated idempotency key returns the Project and does nothing again, even
  when its revision is stale by then. The same key with a different request
  is refused.
- When the registry database cannot be opened, every operation raises
  `RegistryUnavailable`. No cached or stale read is offered: a caller that
  cannot reach the registry has no answer.

### Codes and rotation

- A code is optional, in or out of rotation. A blank code is no code.
- The spelling is kept as entered. Codes are compared with the lookup
  normalization (`Normalizers.code`: case, spaces and punctuation ignored), so
  `BH-101`, `bh 101` and `BH101` are one code. A code with no letter or digit
  is refused.
- **Rotation** is `in`, `out` or `unclassified`, and is never derived from
  production Status. A new Project is `out` unless the registration says
  otherwise.
- Across all of T6, at most one Project in rotation holds a code. The check
  and the change are one transaction, and a unique index backs it, so of
  several concurrent claims exactly one wins.
- Leaving rotation keeps the code on the Project's record and frees it for
  another Project. A Project that returns keeps its ID. If its code is held by
  then, the return is refused with `CodeHeld` until someone changes or clears
  its code, or takes the holder out of rotation.
- `unclassified` is for imported Projects (t6-core#71) that nobody has
  reviewed. An unclassified Project blocks its recorded code for everyone else,
  including other unclassified Projects with the same code. `set_rotation` to
  `in` or `out` classifies it. Nothing can be set back to `unclassified`.

### Bindings

- A page is bound to at most one Project. Notion page IDs are compared
  without dashes or case.
- `bind_page` makes a page the Project's **active** binding. The page it
  replaces stays as a **legacy** binding and still resolves to the same
  Project with `get_by_page`. A page that was ever bound to one Project is
  never bound to another.

### Notion sync and reconciliation

The registry never calls Notion. An accepted change is committed first and
answered with `sync: pending`; a worker shows it in Notion afterwards.

- **Out to Notion.** The server queues one `registry.sync` job per sync
  intent on the `main` queue: at start, after each registry request and before
  each claim. A crash between the commit and the queueing loses nothing. The
  worker reads the Project as it is *now* and writes three properties to its
  active page: `T6 ID` (rich text), `Project Code` (select) and `Rotation`
  (select). An old or repeated job therefore writes current values, never old
  ones.
- **Failure.** A Notion failure never undoes the change. The job is retried
  (five attempts, 30 seconds apart) and the Project shows `sync: pending` with
  the Notion error code in `sync_error`. After the last attempt it shows
  `sync: failed`. The next accepted change or reconciliation of that Project
  starts a new sync.
- **In from Notion.** A `notion.page_changed` job reads the page as it is now
  and reports what it shows. The event's own content and order are not
  trusted, so duplicates and late events are harmless.
  - A registry-owned value that differs from the registry is put back, and a
    `projection_conflict` review item records what was shown. Notion never
    changes a Project. While a sync is already on its way, a difference is
    not a conflict.
  - Descriptive fields (name, Status, dates and the rest) are Notion's and are
    left alone.
  - A page in the Projects data source with no binding becomes one
    `unbound_page` review item. It never becomes a Project by itself and is
    never matched by code or name. Registering or binding it closes the item.
  - A page that is in the trash or that Notion no longer returns marks its
    binding `available: false`, writes a `page_unavailable` audit entry and
    opens a `page_unavailable` review item. The Project, its ID, code,
    rotation and bindings stay. A restored page is reconciled through the
    same binding (`page_restored`).
  - A page outside the Projects data source is ignored.
- **Review items** are open until their cause goes away:
  `GET /registry/review-items`.
- **Operator.** `POST /registry/pages/{page_id}/reconcile` queues the same
  reconciliation for one page, for a missed webhook.

Set-up in Notion, not yet done or checked against the live workspace
(t6-core#13, t6-core#21): add the `T6 ID` (text) and `Rotation` (select: `in`,
`out`, `unclassified`) properties to the Projects data source, and subscribe
the webhook to `page.created`, `page.deleted`, `page.undeleted` and
`page.properties_updated`. Until the properties exist every sync fails with
`validation_error` and is reported, and no Project is affected.

### Migrating existing Projects

`t6.registry.migration` registers the Projects that already exist in Notion,
without breaking anything that points at their pages. Run it once, beside the
registry file: the CLI opens the SQLite file directly, so run it on the host
that serves it. SQLite takes turns between it and the running server.

**Rollout order**

1. Deploy a build with the registry (t6-core#68 to #70) and add the `T6 ID`
   and `Rotation` properties to the Projects data source
   ([Notion sync](#notion-sync-and-reconciliation)).
2. **Back up** the registry file (`sqlite3 t6-registry.sqlite ".backup
   backup.sqlite"`) and note the Notion export you would restore from.
3. **Preview**: `t6 admin registry migrate --data-sources sources.json > preview.json`.
   `sources.json` maps `projects` (required) and `job_orders`, `deliverables`,
   `external_links` to data source IDs. The preview lists each distinct page,
   whether it would be registered or is already, how many Job Orders,
   Deliverables, External Links and Episodes point at it, the codes shared by
   several pages and the pages with no code. A reference kind whose data
   source was not given is listed under `references_not_counted`, never shown
   as zero. Drive Assignments are always listed there: they cannot be counted
   per Project yet. Read the preview before the next step.
4. **Apply**: `t6 admin registry migrate --data-sources sources.json --apply --preview
   preview.json --actor <name>`. It registers every page that has no Project,
   **unclassified**, with its recorded code, and prints a report. It refuses
   (exit 2) when the pages differ from the preview. Exit 1 means some pages
   could not be registered (for example a code with no letter or digit); fix
   them in Notion and run it again. A rerun keeps every ID it already issued
   and registers only what is new.
5. **Classify**: write a JSON object of T6 ID (or page ID) to `in` or `out`
   and run `t6 admin registry classify --decisions decisions.json --actor <name>`.
   Rotation is a decision, never derived from Status. `GET
   /registry/unclassified` lists what is left. Only unclassified Projects
   change; a classified Project is never reclassified here.
6. **Verify**: for each Job Order, Deliverable, External Link and Drive
   Assignment of a sample of Projects, read it by T6 ID and by its page ID and
   check they give the same rows (`ProjectRefs`, below). Then let the
   [Notion sync](#notion-sync-and-reconciliation) show the T6 IDs in Notion.

**Rules the migration keeps**

- One Notion page is one Project. Rows are never merged by code or name, so
  repeated and blank codes each get their own ID, and the shared codes are
  listed in the preview for a person to see.
- Until classified, a Project blocks its code. Two pages with the same code
  therefore block each other: classify the one that is finished `out` in the
  same batch (out decisions are applied first), or the other is reported as a
  conflict with `held_by`. Nothing is freed or reassigned until then.
- Every step is repeatable and audited with the `--actor` you gave.

**Old callers keep working**

`ProjectRefs(registry)` resolves a T6 ID, a page ID or a replaced page's ID to
one Project: `identity(ref)`, `pages(ref)` (active page first, then replaced
ones), `same(a, b)` and `identified(project)`. A reference the registry does
not know is an unmigrated page and stands for itself. It reads the registry on
every call and raises `RegistryUnavailable` when there is none.

- `Lookup(index, same_project=refs.same)`: `deliverable(text, project_id=)`
  takes either form.
- Media locations belong to the T6 Project, so they stay attached when its
  Notion page is replaced; `MediaLink.pages` lists the active page first.
- `InMemoryDriveStore(same_project=refs.same)` finds a Project's open
  Assignments under either form. A Drive store you write should match
  `Ref.page_id` the same way.
- Project Start events stay keyed by page ID (`ProjectStatusChanged`, its job
  dedupe key and payload are unchanged). A flow that needs the Project calls
  `refs.identity(event.page_id)`; adapting the flow itself is t6-core#73.

The legacy form is not removed. Job Orders, Deliverables and External Links
rows still hold page IDs in Notion and keep working through the registry's
stored bindings, including the bindings of pages that were replaced. Remove it
only after every caller above passes a `ProjectRefs`.

### Service routes

The routes are on the tailnet listener only. The caller sends
`Authorization: Bearer <secret>`. A registry grant is separate from a queue
grant. The main worker's secret may read and use the two `sync` routes; it
gets 403 on every route that changes a Project. The audit actor is the name of the
grant that made the change, never a value from the request.

| Route | Grant | Body | Answer |
| --- | --- | --- | --- |
| `POST /registry/projects` | write | `{}`, with optional `notion_page_id`, `code`, `rotation` | 201 `{"project": {...}, "created": true}`, or 200 with `"created": false` for a repeated key |
| `POST /registry/projects/{id}/code` | write | `{"code": "ABC", "expected_revision": 1}`; `null` clears the code | 200 `{"project": {...}, "changed": true, "replayed": false}` |
| `POST /registry/projects/{id}/rotation` | write | `{"rotation": "in", "expected_revision": 2}`; `in` or `out` | as above |
| `POST /registry/projects/{id}/bindings` | write | `{"notion_page_id": "...", "expected_revision": 3}` | as above |
| `GET /registry/projects/{id}` | read | | 200 `{"project": {...}}` |
| `GET /registry/projects/{id}/audit` | read | | 200 `{"entries": [...]}`, oldest first |
| `GET /registry/pages/{page_id}` | read | | 200 `{"project": {...}}` for an active or legacy binding |
| `GET /registry/review-items` | read | | 200 `{"items": [...]}`, open items, oldest first |
| `GET /registry/unclassified` | read | | 200 `{"projects": [...]}`, Projects not yet classified as in or out of rotation, oldest first |
| `POST /registry/pages/{page_id}/reconcile` | write | `{}` | 200 `{"job_id": 7, "created": true}` |
| `POST /registry/projects/{id}/sync` | sync | `{"revision": 3, "ok": false, "detail": "validation_error", "attempt": 2}` | 200 `{"project": {...}}` |
| `POST /registry/pages/{page_id}/observed` | sync | `{"available": true, "shown": {"t6_id": "...", "code": "ABC", "rotation": "in"}}` | 200 `{"outcome": "in_sync", "project": {...}}` |

Every `POST` with the write grant needs an `Idempotency-Key` header and a
JSON object body. The two `sync` routes are for the worker and need no key:
both only record what Notion shows.

A project in an answer:

```json
{
  "id": "t6p_0f3a9c1d2b4e5f6a7b8c9d0e",
  "revision": 3,
  "code": "ABC",
  "rotation": "in",
  "bindings": [
    {"system": "notion", "page_id": "<32 hex characters>", "bound_at": 1700000000.0, "status": "active", "available": true}
  ],
  "sync": "pending",
  "sync_error": null,
  "created_at": 1700000000.0
}
```

`sync` is `pending` while an accepted change has not reached Notion, then
`synced`, or `failed` when the sync used up its attempts; `sync_error` says
why the last attempt failed. Times are seconds since the epoch. A binding's
`status` is `active` or `legacy`; `available` is false while its page is
deleted or out of reach. A review item has `id`, `kind`, `page_id`,
`project_id`, `detail` and `created_at`. An audit entry has `project_id`,
`action` (`registered`, `page_bound`, `code_changed`, `rotation_changed`,
`classified`, `page_unavailable` or `page_restored`), `actor`,
`at`, `before` and `after`.

| Status | Meaning |
| --- | --- |
| 400 | no `Idempotency-Key`, body is not a JSON object, or a missing or wrong-typed field (`expected_revision` is required on a change) |
| 401 | missing or unknown secret |
| 403 | the secret has no registry grant for this route |
| 404 | no such Project, page or route |
| 409 | `{"error": "code_held", "code": "...", "held_by": ["t6p_..."], "reason": "in_rotation"}` (or `"unclassified"`) |
| 409 | `{"error": "revision_conflict", "revision": 4}`: read the Project again |
| 409 | `{"error": "page_already_bound", "project_id": "..."}` or `{"error": "idempotency_key_reused"}` |
| 503 | `{"error": "registry_unavailable"}`, or the server was started without a registry |

### Operator actions

An authorized person registers, binds and classifies Projects from Sixto cards
in Telegram. Core owns the rules; the gateway only carries a card out and a tap
back. Two cards:

- A Projects page no Project owns: **New Project** registers it out of rotation
  with no code, **Existing Project** binds it to a Project that has no working page
  (the old page stays as a legacy binding), **Skip** leaves it.
- An unclassified Project: **In rotation**, **Out of rotation** or **Later**.
  A held code refuses *In rotation* and changes nothing.
- `/project` in the operator's chat with Sixto lists Projects in rotation;
  **Historical Projects** switches to the ones that are not. Each is named by its
  T6 ID. Picking one offers **Put in rotation** or **Take out of rotation**,
  **Change code** (then send `/project NEWCODE`) and **Clear code**.
- A Project that cannot enter rotation because another holds its code changes
  nothing and asks: **Clear this Project's code and enter**, **Change this
  Project's code**, **Take the holder out of rotation**, or **Cancel**. A
  reservation never moves without one of these.

A tap changes the registry only from an operator on `T6_OPERATOR_IDS`, from the
chat and message the card was sent to, once, before the card expires, and only
if the Project is still at the revision the card was made from. Anything else
changes nothing and is recorded as a rejection. The registry actor is
`telegram:<user id>`. The card is edited to show the outcome, and later whether
Notion shows it yet (pending, updated, or failed after retries; the change stands
either way). Cards and their state are kept in `T6_OPERATOR_DB`, so a restart
loses nothing.

`docs/operator-actions.md` is the contract the gateway's workflows consume.

### Deployment, health and recovery

`docs/deployment.md` is the runbook for the VPS: process layout, configuration,
rollout order, `t6 health`, `t6 admin backup` and `t6 admin restore`, and a verification log
that separates what the tests prove from what still has to be run on the VPS.

## Drives

`t6.drives` models physical external drives (never Google Drive). It does no
I/O and needs no extra.

- A `Drive` holds serial, brand, model, capacity in TB, format, form factor
  (`FormFactor.PORTABLE` or `FormFactor.POWERED_DESKTOP`), owner (a Studio page
  ID, or `None` for T6), purchase date, warranty and remarks.
- Every Drive has a permanent opaque `id` at construction; `key` is its alias.
  Equality and all store references use that ID, never a serial. A meaningful
  label, description, physical note or serial is required, not all individually.
- `serial` is the optional accepted serial. `normalize_serial` removes notes in
  brackets, trims and upper-cases, so `" abc123 (old) "` is `"ABC123"`.
  `find_by_serial` explicitly looks up accepted serials only.
- `set_serial(drive_id, serial)` adds/corrects the accepted serial; `None`
  removes it. A collision raises `DuplicateSerial` without any changes.
  `suspected_serial` is separately uncertain and can conflict; only explicit
  `promote_suspected_serial(drive_id)` accepts it after checking uniqueness.
  `set_review` manages a general flag/note. None of these restrict workflows.
  See [the identity API and compatibility guide](docs/drive-identity.md).
- `parse_capacity_tb` reads sheet text: `"4TB, P"` is 4.0 and `"500GB"` is 0.5.
  Text it cannot read raises `ValueError` naming the text.
- `DriveStore` is the interface the custodian's web app implements (app spec:
  [mclbautista/t6-drives#1](https://github.com/mclbautista/t6-drives/issues/1)):
  `add_drive` and `get_drive(drive_id)`. Adding a serial that is already recorded
  raises `DuplicateSerial`. `t6.drives.testing.InMemoryDriveStore` is the fake
  for tests.
- Custody is an append-only log of `CustodyEvent`s: kind (`CustodyKind.RECEIVED`,
  `RELEASED`, `RETURNED`, `RETIRED`), date, the `Party` involved (a Contact or
  Studio), the Project, who logged it and an optional reason. `RETIRED` needs a
  reason (dead, lost, sold). `DriveStore.append_event(drive_id, event)` and
  `list_events(drive_id)` are the only ways in and out: events cannot be edited or
  deleted, so a correction is a new event and both stay in the list.
- A shipment is several Drives released in one handover.
  `DriveStore.release_shipment(drive_ids, on=..., party=..., project=...,
  logged_by=...)` appends one released event per Drive and returns the shipment
  ID they share; if any ID is unrecorded, nothing is released. Released
  events appended one by one can also carry a `shipment_id`.
  `shipment_events(shipment_id)` lists a shipment's events with their Drives.
- `shipment_manifest(store, shipment_id)` gives the manifest data from the
  store alone: one `ManifestLine` per release with permanent `drive_id`, accepted
  `serial`, separately `suspected_serial`, description, review state, label, capacity in TB,
  recipient, date and who logged it. The label is the one from the Assignment
  the Drive was on at the release date, so a manifest made after a relabel
  still shows what went out. It is data, not a printable page.
- `DriveStore.status(drive_id)` is derived from the last appended event
  (`DriveStatus.IN_CUSTODY`, `RELEASED` or `RETIRED`) and never stored. A Drive
  with no events has no status (`None`). `derive_status(events)` does the same
  over a list. An unrecorded ID raises `UnknownDrive`.
- Contacts, Studios and Projects are a `Ref`: a Notion page ID, or, when a name
  could not be resolved, the name as text with `unresolved` True so it can be
  reviewed.
- An `Assignment` holds label, `project` (a `Ref`), `role` (`Role.MASTER`,
  `BACKUP`, `OFFLINE`, `SWING` or `ARCHIVE`), start, end and remarks. No end
  means the Assignment is open. `role` and `start`, and a custody event's
  date and `party`, are `None` when they were never recorded, as in imported
  rows. A label may also be `None`. `DriveStore.add_assignment(drive_id, assignment)`
  adds one and never edits or closes earlier ones. `open_assignments(project)`
  lists the Drives still out for a Project; `assignment_history(drive_id)` lists a
  Drive's Assignments across Projects, oldest start first. An unknown ID
  raises `UnknownDrive`. A label can repeat on different Drives and Projects.
- A swing Assignment (role `SWING`) may carry a `return_date`, the day it is due
  back, and a `holder` (a T6 Person ID). `return_date` cannot be before the
  start. An Assignment may have no Project (`project` is None) only when it has a
  holder; a second open Assignment for the same Drive, role and holder is refused
  when either has no Project. `DriveStore.resolve_person(contact_page_id)` gives
  the Person bound to a Contacts page. `DriveStore.drop_return_date(drive_id, number)` clears it,
  `overdue_returns(today)` lists the open Assignments due back before `today`,
  and `check_person(person_id)` checks a holder.

### Drive reads and inventory verbs

The reads are open to any active staff Person, read only, and so also MCP tools: `t6 drive list [--q TEXT]
[--serial S] [--needs-review true|false] [--project T6P] [--status S]` (the filters of the Drive web
app's inventory screen, combined with AND; `--q` searches the Drive ID, serials, label and
description, and `--project` finds Drives with an open Assignment for that Project), `t6 drive get
--drive ID` (facts, status, custody events in append order, numbered Assignments, source rows and
linked Deliveries), `t6 drive find --serial S` (accepted or suspected serial, in any spelling),
`t6 drive shipments`, `t6 drive shipment --shipment ID` and `t6 drive exceptions` (the import
exception queue). A Drive with no custody events has the status `unknown`.

The writes need the `drives` permission, preview, then confirm with `--confirm`, and are idempotent
by key: `t6 drive add --label L [--serial S] [--brand B] ...` registers a Drive (a serial already
recorded is refused, naming the Drive that holds it), `t6 drive edit --drive ID [--label L]
[--form-factor F] [--serial S] [--suspected-serial S] [--needs-review true|false] [--review-note N]`
changes only the facts named (an empty value clears one), and `t6 drive promote-serial --drive ID`
accepts the suspected serial. Custody events and Assignments are not touched.

### Swing Drives

Swing Drives live once, in `t6.drives`. The verbs, all with the `drives`
permission except the read, are `t6 swing assign DRIVE TARGET [--project P]
[--label L] [--start D] [--return-date D]` (TARGET is a T6 Project ID, or a T6
Person ID; a Person needs no Project, and `--project` may be added), `t6 swing unassign DRIVE [--project P]
[--person T6N] [--assignment N] [--end D]` and `t6 swing return DRIVE [...]` (the same
options). Each previews, then confirms with `--confirm`, and is idempotent.
`return` ends the swing and clears its return date; `unassign` ends it and
keeps the date in history. When a Drive has several open swings, name one with
`--project`, `--person` or `--assignment`, or the verb answers Ambiguous. `t6 swing
overdue` (read only) lists the open swings due back before today in Manila time.

`t6 admin drive import --swing --data-sources FILE` (with a `swing_drives`
entry) previews the import of the Notion Swing Drives database and writes
nothing. `--apply --preview FILE` carries it out, and only if Notion still
holds the data the preview saw. A second apply changes nothing. The import
reads the live properties by name (Name, In Use, Last Moved, Collaborator,
Format, HD Type, Short Notes). Every row creates one Drive keyed by its page
ID, with no serial, unknown ownership and its label from Name; a name or label
never matches another Drive. Format, HD Type and Short Notes fill the Drive's
existing `format`, `description` and `remarks`. A row not in use is a Drive with
no Assignment. A row in use with exactly one Collaborator that is a registered
Person (its Contacts page is bound to a Person) and a Last Moved date gets an
open swing held by that Person from that date, with no Project. Any other
holder case still creates the Drive and puts the holder question in the
exception queue with its page ID. Person-In-Charge and For Bawi are not read.

### Importing the legacy workbooks

`t6.drives.importer` needs the `drives` extra. It provides import and local
review capability; a real-data import, workbook freeze and cutover require
separate human approval. No such operation is performed by this change.

```python
from t6.drives.importer import import_per_project_workbook

result = import_per_project_workbook(path, store, lookup=lookup, logged_by=Ref(text="workbook import"))
```

- The per-project workbook has one tab per Project: an optional
  `PROJECT NAME: ...` or `PROJECT: ...` row, a header row starting with `HARD DRIVE LABEL`, then
  one row per drive. A tab with no project name row uses the tab title.
- A row becomes a Drive (brand, capacity, format) with an Assignment for the
  tab's Project, a received event when the row has a received date and a
  released event when it has a release date. The release recipient is the
  person in charge or, failing that, the remarks, kept as text.
- The Project is a page ID only when `t6.lookup` finds exactly one Project
  with that name; otherwise the name is kept as text. The role is read from
  the label only when it names exactly one role; otherwise it is `None`.
- Nothing is guessed. `result.review` is the review report: one `ReviewItem`
  (workbook absolute path, tab, row number, reason, detail) for everything a
  person has to look at. Keep reports local: they may contain private data.

  | Reason | What happened to the row |
  | --- | --- |
  | `pre-header line` | unparsed non-empty line preserved as evidence; no release facts guessed |
  | `no label` | imported if another meaningful identifying detail exists |
  | `note in serial cell` | imported with suspected candidate and original cell preserved |
  | `shared serial` | distinct review-flagged Drives with suspected serials; never merged |
  | `unreadable` | imported and flagged with the unreadable or conflicting capacity or date left blank; not imported only when no other identifying detail is left, or a movement's `IN/OUT` cannot be read |
  | `blank serial` | imported as a full Drive with permanent ID if meaningful |
  | `old and new labels` | imported with its label as written; the detail names its pair |
  | `unresolved project` | its rows are imported with the name as text; listed once, at the row where the Project's block starts |
  | `movement with no serial` | one traceable review item per movement; not attached by label |
  | `ambiguous serial` | movement has multiple candidates; full row preserved for review |
  | `additional drive facts` | clean serial matched, but additional physical observations need review; original record is not overwritten |

- It writes only through `DriveStore.import_row`, which commits each row and
  its receipt atomically. Unchanged reruns at the same path preserve IDs and
  add nothing. Different rows with identical movement facts remain distinct.
  See the [receipt and adapter contract](docs/drive-identity.md#import-receipts).
- `SPACE` aliases `TOTAL DISK SPACE` using the existing capacity parser;
  invalid or conflicting values are reported. All duplicate `REMARKS` columns
  are preserved. `T6 IN` records receipt, with no invented date when undated.
- `import_log_sheet_workbook(path, store, lookup=..., logged_by=...)` reads the
  log sheet workbook and returns the same kind of result. Its tabs are told
  apart by their header row:
  - An inventory tab (swing drives, archive drives) has a serial column. A row
    becomes a Drive with an Assignment under the tab's title. Remarks that say
    DEAD become a retired event with the remark as the reason and no date.
  - A released-batch tab has a `DATE: ...` line above its header. Every Drive
    on it gets a released event on that date and they share one shipment ID.
    A row with a label, no serial and a quantity is a group heading: the
    Project of the rows under it.
  - A monthly log tab has `DATE` and `IN/OUT` columns and no serial column. A
    movement is imported only when its remarks hold one `S/N: <serial>`: IN and T6 IN are
    received event, OUT is a released event to the RECEIVED BY name. Releases
    on one date to one name share a shipment ID. The other movements are not
    imported, because a label alone does not identify a Drive.
  - A Drive already in the store, for example from the per-project workbook,
    keeps its record and gains the new Assignment and events.
- `t6 admin drive import WORKBOOK [--log-sheet FILE] [--projects FILE]` is the
  preview: it runs the import into an in-memory store that is thrown away and
  prints `{"digest": ..., "imported": n, "review": [...]}`. `WORKBOOK` is the
  per-project workbook. Nothing is written anywhere. The `digest` is a SHA-256
  over the bytes of the files given, each under its role, so the same files give
  the same digest, and a changed, added or dropped file gives another.
  `--projects` is a JSON list of `{"page_id", "name"}` to resolve Project names
  against; without it every Project is reported as unresolved. The preview has no
  registry and treats every listed Project as resolved.
- `t6 admin drive import WORKBOOK [--log-sheet FILE] [--projects FILE] --apply
  --preview FILE --actor NAME [--registry-db FILE]` carries it out. Save the
  preview's output to a file and pass it as `--preview`; the apply is refused,
  and writes nothing, when the digest of the files as they are now is not the one
  in that file. It runs the same import into the t6 database at `--registry-db`
  (default `$T6_REGISTRY_DB`) and writes the Drives, Assignments, custody events
  and exceptions. `--actor` is recorded as the one who logged the custody events.
  In an apply a Project is resolved only when its Notion page is bound to a
  Project in the registry; a name that is not, even one listed in `--projects`,
  is kept as text and becomes an `unresolved project` exception. The apply
  prints the report with a `written` object (`drives`, `assignments`, `events`,
  `exceptions`: the rows it added). Applying the same files again adds nothing.
  Receipts are keyed by the workbook's absolute path, so keep the files where
  they were. `drive list` and `drive exceptions` show the result. The real
  workbooks, the projects file, the previews and the database never go into a
  repository.

## Lookup

`t6.lookup` turns a loose mention into the right entity, over an `Index` of
`t6.domain` entities the caller supplies (no I/O). `Lookup` has one method per
entity type: `job_order(telegram_key)`, `project(code, name=)`,
`project_by_id(id)`, `deliverable(text, project_id=)`, `contact(mention, team=)`,
`machine(text)` and `studio(name)`. Each runs its pipeline cheapest first (exact ID,
normalized, fuzzy, optional LLM `judge`, human review) and returns a
`Resolution` (entity or none, method, score, ranked candidates,
`needs_review`). Below the policy thresholds it never picks. Deliverables are
resolved within one Project; Contacts within the Project team first, then all.
`Lookup.propose_alias` returns an `AliasWrite` for Notion `Aliases`; it never
writes. Honorifics and other normalizers are configuration (`Normalizers`).
The default fuzzy scorer is stdlib-only; set `Policy(scorer=...)` to use
another. The exception is `contact`, which follows the studio's matching policy
(see `CONTEXT.md`) and so scores with rapidfuzz's WRatio: install
`t6[lookup]`. Without it a Contact lookup that reaches the fuzzy step raises
`ImportError`. `suggest_contacts(mention, team=)` returns up to 3 Candidates
for a person to choose from; it never decides.

### Reusable Project Codes

A Project Code can belong to several Projects over time, so `project(code)`
looks at rotation. Build the index with `ProjectRefs.identified(project)`, which
fills in each registered Project's T6 ID, rotation and code from the registry:

| Projects with the code | Result |
| --- | --- |
| exactly one in rotation (any number out) | that Project, `needs_review` false |
| none in rotation | `needs_review`, `reason="no_in_rotation_holder"`, every Project with the code as candidates |
| several in rotation | `needs_review`, `reason="several_in_rotation_holders"`, the holders as candidates |
| any unclassified | `needs_review`, `reason="unclassified"`, all as candidates |
| not registered yet (no rotation known) | as before: one match decides, several need review |

Codes compare with the same normalization the registry reserves with, so
`BH-101` and `bh 101` are one code; the Project keeps the spelling it was
entered with. `project_by_id(id)` takes a T6 ID, or any page the Project was
bound to, and returns that Project whoever holds its code now: this is how a
reviewer selects a historical Project. Existing relationships are never looked
up by code. `Resolution.reason` is `None` for every other lookup.

`project_in_text(text)` finds a Project Code inside longer text, such as
`AB-101 pilot cut`. It scans the registry's codes as whole tokens (no word
character on either side), case-insensitive, longest code first, blanking each
matched span so a shorter code inside a longer one does not match again. No code
gives no match with `needs_review` and `reason="no_code_in_text"`. Several
distinct codes give `reason="several_codes_in_text"` with their Projects as
candidates. Exactly one code gives the answer of `project(code)`, so the table
above applies. It never falls back to a Project name.

## Fleet

`t6.fleet` (install `t6[notion]`) reads Bays as `Machine` and writes the fleet
facts. The rules are in `CONTEXT.md` under Fleet.

```python
from t6.fleet import Fleet, MachineFacts
from t6.domain import Ring

fleet = Fleet(notion, bays_data_source_id)
machines = fleet.machines()
results = fleet.sync(
    [MachineFacts(names=("201-Bay-01-Mac-Pro-111", "201 Bay 01 Mac Pro"), serial_number="C02...", ring=Ring.PILOT)],
    apply=False,  # a dry run; True writes
)
```

Each `SyncResult` says `matched`, `unmatched` or `ambiguous`, the changes, the
facts skipped (unknown or stale), and, when applied, the saved `Machine` or the
typed `Rejected` or `Ambiguous` failure. `t6.fleet.rules` has `match_key`,
`role_group`, `manifest_chain` and `is_stale`. Run `t6 admin snapshot` once so
the Ring options are kept by ID.

## Optional extras

Install only the adapters an application needs, for example
`pip install 't6[notion]'`.

| Extra | Purpose |
| --- | --- |
| `notion` | Notion client and models |
| `privacy` | Withholding and reversible masking |
| `llm` | Typed LLM tasks and transports |
| `frameio` | Frame.io integration |
| `resolve` | DaVinci Resolve integration |
| `instagantt` | Instagantt export reader |
| `lookup` | rapidfuzz, for fuzzy Contact matching |
| `drives` | One-off importer for the legacy drive workbooks |
| `server` | Webhook server and queue (stdlib only) |
| `worker` | Job workers (stdlib only) |

The `notion` extra installs `httpx`, the `lookup` extra installs `rapidfuzz`, the `resolve` extra installs `psycopg` and the `drives` extra installs `openpyxl`.
The `privacy`, `instagantt`, `server` and `worker` extras are implemented and
need no third-party packages. The `frameio` extra holds the
[Frame.io worker](#frameio) and needs none; its briefing feed also needs the `notion` extra. The `notion` extra does not install Frame.io, Adobe sign-in, or
LLM libraries.

## Frame.io

One process, the **Frame.io worker**, holds the Frame.io token. Adobe rotates
the refresh token on each use, so a second holder would sign the first one
out. Every other part of T6 asks the worker through the `frameio` queue and
holds no Frame.io credentials.

```python
from t6.frameio import Ok, ShareLinks, ask
from t6.worker import HttpTransport, QueueClient

result = ask(QueueClient(HttpTransport(server_url), secret), ShareLinks(frameio_project_id="..."))
if isinstance(result, Ok):
    links = result.value
```

- **Requests.** `FrameioProjects`, `ShareLinks`, `Comments`, `Versions`, `Approvals`,
  `BriefingFeed(since)` and `CreateProjectFolders(project_id, name)` (Project
  Start's Frame.io folder step, which must be safe to run twice). Each is
  one job on the `frameio` queue.
- **Results.** `ask` returns `Ok(value)` or `Failed(reason)` and never raises.
  No answer within `timeout` seconds (default 30), a refused queue call and a
  Frame.io error are all `Failed`. Each `ask` is a new job, so asking again
  asks Frame.io again. `value` is JSON. `decode(ShareLinks, result.value)`
  turns it into typed objects: `FrameioProject`, `ShareLink`, `Comment`,
  `Version`, `Approval` or `BriefingShow` (see `t6.frameio.model`).
- **Worker.** `FrameioWorker(tokens, run, notify)` gives the handlers for the
  `frameio` queue. `tokens` is the token store (t6-core#15) and `run` executes
  one request against the Frame.io API (t6-core#16). `frameio_backend` in
  `t6.worker.__main__` builds the pair from the environment. `tick()`
  refreshes the token once a day.
- **Reads.** `t6.frameio.reads.FrameioReads` runs a request against Frame.io
  V4 (`https://api.frame.io/v4`). It finds the T6 account from the token; set
  `T6_FRAMEIO_ACCOUNT_ID` if the sign-in sees more than one account. A share
  link is live when it is enabled and not expired. `Versions(file_id)` gives
  the files of the version stack the file is in, or the file alone.
  `Approvals(file_id)` gives the file's `Status` metadata field, because
  Frame.io V4 has no separate approval object. A 401 gets one token refresh
  and one more try. A 429 waits and tries again, five times at most.
  `CreateProjectFolders` is not built (t6-core#23) and is answered `Failed`.
- **Briefing feed.** `BriefingFeed(since)` answers, per Frame.io project, the
  live share links whose latest activity is after `since`. Activity is the
  later of the share's last view and its newest entry in Frame.io's activity
  list. Each group carries the Notion Projects pages its Frame.io project is
  linked to (the Media location of kind `frameio-project`), or none. The feed
  writes nothing, neither to Notion nor to t6: share links have no row and no
  Last Activity stamp, because activity is read from Frame.io each time, and
  every live share is reported `active`. The feed needs `T6_API_URL` (the t6 API
  on Sixto) and `T6_REGISTRY_READ_SECRET` (its read-only registry grant) on the
  Frame.io worker; one without the other stops the worker at start-up.
  A feed over many share links can
  take minutes because of Frame.io's rate limit, so give `ask` a long
  `timeout`.
- **Read API.** With `T6_FRAMEIO_READ_BIND` and `T6_FRAMEIO_READ_SECRET` set,
  the Frame.io worker also serves read-only HTTP (`t6.frameio.readapi`), so an
  app such as notion-reports gets Frame.io data with a read secret and no
  Frame.io token. Routes, all `GET` with `Authorization: Bearer <secret>`:
  `/frameio/status`, `/frameio/projects`, `/frameio/projects/<id>/share-links`,
  `/frameio/files/<id>/comments`, `/frameio/files/<id>/approvals`,
  `/frameio/files/<id>/location` (folder and version stack) and
  `/frameio/briefing?since=<ISO 8601 time with zone>`. Nothing else is
  routed: no write, no token. A signed-out worker answers 503. The client
  `FrameioReader(HttpTransport(url, timeout=120), secret)` returns typed objects
  (`projects()`, `share_links(id)`, `comments(file_id)`, `approvals(file_id)`,
  `briefing(since)`) or `Failed(reason)`, and `SignInNeeded` (a `Failed`) when
  a person must sign in; `signed_in()` reads the status. It never raises. Reads
  take turns with queue jobs on the worker, so a long briefing feed delays
  them; give the transport a long `timeout` for it.
- **Tests.** `t6.frameio.testing.FakeFrameio` is an in-memory Frame.io V4 for
  tests, like `t6.notion.testing.FakeNotion`.
- **Token store.** `t6.frameio.tokens.AdobeTokens` signs in to Adobe (OAuth
  Web App, `offline_access`) and keeps the token in one file,
  `T6_FRAMEIO_TOKEN_FILE`, on the Frame.io worker's own volume. The file has
  mode 0600. Only the Frame.io worker makes an `AdobeTokens`; no other
  process reads the file or has the Adobe credential. Adobe replaces the
  refresh token on each refresh, and the new one is stored before it is used.
  An access token within five minutes of its end is refreshed before a
  request uses it.
- **Sign-in.** When Frame.io refuses the token, the worker sends one Telegram
  alert a day with the sign-in link through `notify` (the gateway's `/notify`) and
  fails every request with `Frame.io sign-in needed` until someone signs in.
  Adobe redirects the browser to `GET /frameio/callback?code=...&state=...`
  on the server's public listener. The server queues the code as a
  `frameio.sign_in` job and the worker exchanges it. The server never holds a
  token. The route answers 503 unless `T6_FRAMEIO_WORKER_SECRET` is set.
  The link carries a random `state` that the worker keeps with the token; a
  callback with another state is refused before Adobe is called. The link
  stays the same until a sign-in completes. A worker that starts with no
  token sends the link on its first `tick()`. Adobe unreachable (a 5xx, a 429,
  no network) is not a lost sign-in: the worker tries again in 15 minutes and
  sends no alert.
- **Review message.** Every Frame.io `comment.created` (replies included)
  becomes a `frameio.resolve_comment` job on the `frameio` queue, because the
  webhook names only the comment. The Frame.io worker reads the comment (file,
  author, time) and asks the server's `POST /review/timers` to start or
  restart the quiet timer for the pair (file, author). The route accepts only
  the Frame.io worker's secret. Each new comment from the same person on the
  same file restarts its timer; other people and other files have their own
  timers. The wait is the part of the clip left after the comment (the file's
  `Duration` and `Frame Rate` metadata and the comment's frame, as
  `remaining_seconds`) plus `T6_REVIEW_LOOP_ALLOWANCE_SECONDS` (default 480).
  When the clip length or the comment's position is not known (an image, a
  PDF), the wait is `T6_REVIEW_QUIET_SECONDS` (default 900). A later comment
  never brings the message forward: the timer keeps the longest wait. When a timer runs out, the
  main worker runs `frameio.review_done` (`t6.worker.review.ReviewMessage`)
  and sends one Telegram message through the gateway's `/notify` to
  the first active Project Chat link, or `T6_REVIEW_CHAT` (and `T6_REVIEW_TOPIC`
  if set) when no link is found. The main worker resolves the Frame.io project through its
  Media location (kind `frameio-project`). The message has the file's title, who
  commented, when they started and stopped (Manila time), how many comments they
  made and how many the file has in total, and two links: the newest share
  that is on, not expired and made from the file (its short URL, or "no share
  link"), and the internal Frame.io link for signed-in people. When t6 has a
  Version for the file (`version add`), the message also names it: the
  Deliverable code, the polish note, the Version ID, and "(locked)" when it is
  the locked Version. No share
  password. The title, links and share come from the Frame.io worker through
  the `frameio` queue; when it cannot answer, the job is tried again every 30
  seconds and the third try sends the message with "unknown" in place of what
  is missing. The message is keyed by the job, so a job that runs twice sends
  once. A comment the Frame.io worker cannot read yet (Frame.io down, signed
  out) is tried again every 30 seconds, up to five times. Comments by T6
  staff count too. No Notion lookup.
- **A slow request** holds only the `frameio` queue. The `main` queue keeps
  running, and the caller gets `Failed` at its timeout.

## Instagantt

`t6.instagantt` reads an Instagantt Standalone export saved as CSV (stdlib
only; no Excel). `read_export(path)` / `parse_export(text)` return a
`Timeline` per Instagantt project, each with its `Task`s (name, start, end; a
blank end is a milestone). `link_timelines(timelines, links)`
ties each timeline to its Project through Media locations (kind
`instagantt-project`, ref = the export's Project ID as Instagantt gives it, free
text; kind `timeline` is for `t6t_` Resolve Timelines and is not read here). A
project with no Media location, or with
locations in several Projects, comes back in `unmapped` with
a reason; nothing is guessed. It reads no Notion dates and writes nothing.
The column names are assumed, not yet checked against a live export.

## Notion

`t6.notion` is the one shared Notion client. Install it with
`pip install 't6[notion]'`.

- `AsyncNotionClient` is a generic async httpx client: `retrieve_page`,
  `create_page`, `update_page`, `retrieve_data_source`, and
  `query_data_source` (which follows `next_cursor` and returns every row) or
  `iter_data_source` (which yields rows page by page).
- `NotionClient` is a thin blocking wrapper with the same methods and the same
  results. It runs its own event loop, so do not call it from async code.
- Every request sends Notion-Version `NOTION_VERSION` (`2026-03-11`). Pass
  `notion_version=` to one client to override it for that client only.
- Every failure raises `NotionError` with `status`, `code`, `message` and
  `retry_after`. A timeout or a network error has `status` None and `code`
  `timeout` or `network_error`.
- `t6.notion.codec` decodes property values. `t6.notion.models` turns pages
  into `t6.domain` entities; `project_from_page` builds a Project from a
  Projects page (property names checked against the live schema).
- `t6.notion.testing.FakeNotion` is an in-memory Notion behind an httpx mock
  transport, with `fail_next` and `time_out_next` to inject errors.

```python
from t6.notion import NotionClient, RetryPolicy
from t6.notion.models import project_from_page

with NotionClient(token, retry=RetryPolicy()) as notion:
    project = project_from_page(notion.retrieve_page(page_id))
    rows = notion.query_data_source(projects_data_source_id)
```

### Page content (blocks)

`client.list_block_children(block_id)` returns every direct child block of a
page or block, following the cursor. `client.append_block_children(block_id,
blocks)` adds blocks after the last child and returns the created blocks; more
than 100 blocks are sent as several requests. Both exist on the sync and async
clients (the async client also has `iter_block_children`). The sync and async
clients take the same arguments. An append is a write under the retry policy
below: if a later request of a long append fails, the earlier requests are
already saved.

### Retry policy

This policy is written in ASD-STE100 Simplified Technical English.

1. Retry is off by default. To turn it on, give a `RetryPolicy` to the client.
2. When retry is off, the client does not retry. Each error causes a
   `NotionError` immediately.
3. A read is a page retrieve, a data source retrieve, a data source query, or
   a block children list. A write is a page create, a page update, or a
   block children append.
4. When Notion sends status 429, the client retries the call. This rule
   applies to reads and to writes.
5. When Notion sends a status of 500 or more, the client retries a read. It
   does not retry a write.
6. When a read times out, the client retries the read. When a write times
   out, the client does not retry the write.
7. Other errors cause a `NotionError` immediately. Examples are status 400,
   401 and 404.
8. If a write gets a status of 500 or more, or times out, the client raises
   `NotionError`. The write possibly occurred. Do not send the write again
   before you make sure that it did not occur.
9. Before each retry, the client waits. If Notion sends a Retry-After value,
   the client waits for that number of seconds.
10. If Notion does not send a Retry-After value, the client waits
    `base_delay` seconds (default 0.5). After each retry, the wait becomes
    two times longer. The wait does not become longer than `max_delay`
    seconds (default 8).
11. The client tries a call a maximum of `attempts` times (default 3). This
    number includes the first try. After the last try, the client raises the
    last `NotionError`.

### Schema snapshot and models

`src/t6/notion/schema_snapshot.json` lists every T6 database and the Notion
ID, exact name and type of each property (captured 2026-11-09). Select,
multi-select and status properties also list their option IDs. It holds no
option names, because they contain show and client names. A property or option
keeps its ID when it is renamed in Notion, so t6-core addresses by ID and the
name is for display and errors. After a rename, run
`t6 admin snapshot --data-sources FILE --out FILE` (same file format as
`admin schema-check`) and regenerate; no code changes. A property whose entry has no
ID yet is addressed by name. The `t6.notion.generated`
module has one model per database, such as `BaysRow` and `JobOrdersRow`. Run
`uv run python scripts/generate_models.py` after the snapshot changes. A test
fails when the module is out of date.

Job Order projection requires the `T6 ID` and `T6 Notes` rich-text properties
and their actual Notion IDs. An administrator must create both properties in
the Job Orders data source, run `t6 admin snapshot --data-sources FILE --out
src/t6/notion/schema_snapshot.json`, and run `uv run python
scripts/generate_models.py` before releasing the updated snapshot and model.
Until both IDs are recorded, the worker refuses JO projection with a message
identifying this step.

- A model field maps a Python name to the Notion property ID and exact name
  (a trailing space `Blocker ` or an emoji `🗂️ Coordinator` are kept). Reads
  find the property by ID and writes key it by ID.
- `Model.from_page(page)` ignores live properties that the snapshot does not
  list. A missing or wrong-typed property reads as `None` or an empty tuple.
- `Model.to_properties(**values)` builds the `properties` body for
  `create_page` or `update_page`. A value of `None` clears the property.
  Formula, rollup, timestamp and similar properties are read-only.
- Select, multi-select and status values are sent by name, and Notion adds an
  option that does not exist. Wrap an option ID in `OptionId("...")`
  (`t6.notion.model`) to set it by ID, which survives a rename. The in-memory
  fake does the same.
- Bays carries the Munki fleet fields: `serial_number`, `munki_manifest`,
  `munki_client_version` and `os_version` (text), `ring` (select) and
  `last_check_in` (date).

```python
from t6.notion.generated import BaysRow

body = BaysRow.to_properties(name="Bay 12", os_version="macOS 26.5", ring="pilot")
page = notion.create_page({"parent": {"data_source_id": bays_id}, "properties": body})
bay = BaysRow.from_page(page)
```

### Typed writes

`t6.notion.writes.create_row(client, Model, data_source_id, values)` and
`update_row(client, Model, page_id, values)` write by property ID, read the
page back and compare it with what was written. `values` uses Python
attribute names, as in `Model.to_properties`. Pass `to_domain=` to get a
domain object instead of the row. The result is the saved row, or:

- `Rejected(status, code, message)`: Notion refused the write (any 4xx,
  429 included). Nothing was written.
- `Ambiguous(reason, detail, page_id, mismatched)`: the write may have
  happened. `reason` is `timeout`, `network_error`, `server_error`,
  `readback_failed` or `mismatch` (the saved value differs; `mismatched`
  names the attributes). `page_id` is `None` when a create got no answer.

Nothing is retried and no state is kept: an app decides what to do next with
its own idempotency key, because repeating an ambiguous create can make a
duplicate page. Options set as `OptionId` are checked by ID, other options by
name, and dates by instant to the minute, because Notion drops seconds. Pass
a timezone with a time of day.

Credentials are withheld in every text t6-core writes to Notion. The client
passes each rich-text part of a page's properties, its body and appended
blocks through `t6.privacy.withhold` (labelled `password`, `pin` and similar
values become a placeholder; URLs stay intact), so no caller can forget it.
Names are written unchanged. `create_row` and `update_row` compare the
readback with the withheld text.

### Job Orders

`t6.notion.job_orders.JobOrders(client, data_source_id)` reads and creates Job
Orders as `t6.domain.JobOrder`, with every field the apps use: number,
department, date, deadline, title, subtitle, task, purpose, source, platform,
link settings, watermark, mode of transfer, blocker, pending, notes, watchdog
notes, telegram key, status, health (read only), the `project_id`,
`assigned_ids`, `cc_ids`, `coordinator_ids` and `deliverable_ids` relations,
and `source_text`, the page body. Notion property names stay in
`t6.notion.generated`. `get(page_id)`, `find_by_telegram_key(key)`, `find_all_by_telegram_key(key)`
(every match, oldest first, for an app recovering from an uncertain create) and
`list()` read; `create({...})` writes through the typed write and returns the saved
`JobOrder`, `Rejected` or `Ambiguous` (a body that could not be saved leaves a
page, so it is `Ambiguous` with that page ID). The only Job Orders property
no field carries is `place`.

`status` and `department` are named values (`JobOrderStatus`, `Department` in
`t6.domain`). `t6 admin snapshot` records each one's Notion option ID in the
snapshot (`named_options`), found by matching the live option name once. After
that an option renamed in Notion is still read and written by ID. A value with
no ID recorded is sent by its label, and a status or department Notion has but
t6-core does not know reads as its plain name. `t6 admin schema-check` reports a
named value whose option is gone under `named_missing`, as drift.

### Meeting Notes

`t6.notion.meeting_notes.MeetingNotes(client, data_source_id)` reads and writes
Meeting Notes pages as `t6.domain.MeetingNote` (title as `name`, meeting date,
project IDs, attendee IDs and names, ready to send, sent at, last edited).
`with_title`, `with_title_prefix` and `ready_to_send` read. `create` saves the
properties through the typed write and appends the body blocks in chunks of
100, returning the saved note, `Rejected` or `Ambiguous` (a body that could
not be saved leaves a page, so it is `Ambiguous` with that page ID). `trash`
and `mark_sent` change a note. A page body is a list of Notion blocks; read it
with `NotionClient.list_block_children`. `append(page_id, blocks)` adds blocks to
an existing body in chunks of 100 and raises `NotionError` when Notion refuses.

The API has four write verbs, role `minutes` (grant it to one service in
`T6_API_SERVICE_PERMISSIONS`; it opens only these four): `meeting-notes create`
(`title`, `meeting_date`, `project_ids` as T6 IDs or Notion page IDs,
`attendee_ids` as Notion user IDs, `blocks`), `append` (`note`, `blocks`),
`trash` (`note`) and `mark-sent` (`note`, optional `at`, default now in Manila).
Body blocks have the shape `meeting-notes list` returns (`kind`, `text`,
`spans`, `checked`, `children`); the kinds are `paragraph`, `heading_1`,
`heading_2`, `heading_3`, `bulleted_list_item`, `numbered_list_item`, `to_do`
and `toggle`, and any other kind is refused. `append` and `trash` are refused
(rule `sent`) for a note that has a Sent at date; `append` is refused for a
note in the trash; `mark-sent` on a sent note returns it unchanged. `create` is
not idempotent: a caller that retries looks for the title with
`meeting-notes list --title` first, and a body that failed leaves the page, so
the outcome is `ambiguous` with its page ID. Each write adds a registry audit
row (`meeting_note_create`, `meeting_note_append`, `meeting_note_trash`,
`meeting_note_sent`) with the page ID and no text. `meeting-notes list` also
takes `latest` (only the note with the newest meeting date after the other
filters, ties to the later last edited, undated notes ignored; one body is
fetched) and `include_body: false` (no block calls, `body: []`).

### Projects, Contacts and Deliverables

`t6.notion.reads` has `ProjectReader`, `ContactReader` (each with `get` and
`list`) and `DeliverableReader.for_project`, returning `t6.domain` entities
addressed by property ID. `list` skips pages in the trash or archived, and
`Project`, `Contact` and `JobOrder` carry `last_edited`, the page's own
last-edited time. `Project` carries status, stage, health, playdate,
submission and archival deadlines, PM and tech notes, `has_post_supe`, and the
role relations as page IDs (`editor_ids`, `assistant_editor_ids`,
`online_editor_ids`, `colorist_ids`, `sound_tech_ids`, `post_coordinator_ids`,
`director_ids`, `cinematographer_ids`). A date range keeps its end in
`playdate_end`, `submission_deadline_end` and `archival_deadline_end` (`None`
when the date has no end; a range end keeps its time as Notion holds it), and
`prod_coor_supe_ids` holds the people in `Prod Coor/Supe`. A date with a time
and no offset takes the `time_zone` Notion holds for it and is an aware
datetime in that zone; with no offset and no zone it stays naive and is the
workspace time. `JobOrder.health` is `None` when the Health formula gave no
value and `""` when it computed an empty string. `Project.notion_project_code`
is the Project Code as Notion shows it; the API's `project_code` is the
registry's value once the Project is registered. `JobOrder.project_ids`
holds every related Project; `project_id` is the first. `Contact.end_of_shift` is a time of day
parsed from "10:00 PM", "10 PM" or "22:00"; "Flexible / Variable", blank and
any other text are `None`, meaning flexible. `Deliverable` carries
`deliverable_class` and `playdate`.

### Schema check

`t6 admin schema-check` diffs live Notion against the snapshot. It needs the Notion
extra, a token in `T6_NOTION_TOKEN`, and a JSON file that maps snapshot
database keys to live data source IDs (keep that file out of the repository):

```console
$ cat data-sources.json
{"projects": "<data source id>", "bays": "<data source id>"}
$ t6 admin schema-check --data-sources data-sources.json
```

The report is JSON. For each database it lists `added`, `removed` and
`changed` (type) properties, and `renamed` properties (same ID, new name),
which are information and not drift. Databases without an ID are listed under
`skipped`.

**Renames and the scheduled check.** `t6 admin schema-check --fix-snapshot` rewrites
the snapshot (`--snapshot FILE`, else the bundled one) with the new names after
renames, then run `scripts/generate_models.py`. If a property was added,
removed or changed in type, it prints the report, exits 1 and changes nothing:
that needs a t6-core change.

The workflow `.github/workflows/schema-check.yml` runs the check every day and
keeps one open issue labelled `schema-drift` titled "Notion schema drift" while
Notion differs from the snapshot. Add two repository secrets for it (a
maintainer step): `T6_NOTION_TOKEN`, a read-only Notion integration token with
access to the databases, and `T6_DATA_SOURCES_JSON`, the data sources JSON
described above. The issue lists database keys and property names and types:
added, removed and type-changed properties, plus renames. It holds no secrets,
option names or Project Codes. The Projects audit is not part of it, because
the job has no registry file.

**Projects are audited against the registry.** When `projects` is in the data
sources, `project_codes` compares the Projects pages with the
[Project registry](#project-registry) at `--registry-db` (default
`$T6_REGISTRY_DB`, then `t6-registry.sqlite`). This supersedes the earlier
rule that every blank or repeated Project Code fails (core #10 and #12): a
Project with no code, in rotation or not, passes, and so does a code that
older Projects keep on record while one other Project holds it. These do not:

```json
{"project_codes": {
  "status": "findings",
  "checked": 42,
  "reasons": [],
  "findings": {
    "active_collisions": [{"code": "ABC", "t6_ids": ["t6p_...", "t6p_..."]}],
    "unclassified": [{"t6_id": "t6p_...", "code": "ABC", "page_ids": ["..."]}],
    "unbound_pages": [{"page_id": "..."}],
    "unavailable_bindings": [{"t6_id": "t6p_...", "page_id": "..."}],
    "missing_pages": [{"t6_id": "t6p_...", "page_id": "..."}],
    "projection_conflicts": [{"t6_id": "t6p_...", "page_id": "...", "detail": {}}]
  }}}
```

- `active_collisions`: several in-rotation Projects share one normalized code.
  The registry prevents it, so this means a damaged or hand-edited file. Take
  all but one out of rotation, or change their codes.
- `unclassified`: imported Projects nobody has classified; they block their
  codes ([classify them](#migrating-existing-projects)).
- `unbound_pages`: a Projects page no Project is bound to. Register or bind it
  explicitly; it is never matched by code or name.
- `unavailable_bindings`: a Project's active page is deleted or out of reach.
- `missing_pages`: a Project's active page is not among the Notion pages and
  nothing flagged it. A replaced page that has gone is not a finding.
- `projection_conflicts`: someone edited a registry-owned value in Notion and
  reconciliation put it back.

`status` is `ok`, `findings` or `incomplete`. **`incomplete` is never a pass:**
it means Notion or the registry could not be read (no registry file was found,
it was locked, or the Notion query failed). `reasons` says which, `findings`
lists what could still be worked out, and `ok` is false. The command does not
create a missing registry file, because an empty one would make every page
look unbound. No stale or cached read is offered; there is nothing to mark as
stale.

The exit status is 0 when everything matches and the audit is complete, 1 when
something drifted, the audit has findings, or the audit is incomplete, and 2
when the command cannot run (for example, no token).

### Cameras

`t6 camera get` and `t6 camera enrich` let a skill read and change Cameras
without Notion property names. Both need the Notion extra, `T6_NOTION_TOKEN`,
and the same `--data-sources` file, which must have a `cameras` entry. Fields
use the generated model's names: `name`, `tags`, `related_to_projects_cameras`
and `wiki`, plus the row `id`.

```console
$ t6 camera get --data-sources data-sources.json            # JSON list
$ t6 camera get --page-id <page id> --data-sources data-sources.json
$ echo '{"tags": ["Sony", "Full frame"]}' | t6 camera enrich <page id> --data-sources data-sources.json
```

`update` reads a JSON object from stdin (or `--json FILE`), writes only those
fields, and prints the updated row. An unknown field, an invalid value, a page
outside Cameras or a Notion error prints `{"error": ...}` on stderr and exits 2.

## Privacy

`t6.privacy.withhold(text)` strips labelled credentials (`pw`, `pass`,
`password`, `passcode`, `pin` followed directly by `:` or `=`) before text
leaves T6. It is one way: the result holds only the masked `text`, the `count`
of values withheld, and the `masker_version`, so nothing can be restored. URLs
are kept byte for byte. It is ported from the watchdog masker in t6-jo-watchdog
and needs no third-party packages or Notion access.

```python
from t6.privacy import withhold

result = withhold("PW: secret")
result.text            # 'PW: [password withheld – see Telegram message]'
result.count           # 1
result.masker_version  # '1.0.0'
```

`t6.privacy.pseudonymize(text, registry, allow)` swaps registry names for
placeholders before text leaves T6, and its `Session` puts them back
afterwards. It uses only the standard library.

```python
from t6.privacy import Person, Project, Registry, pseudonymize

registry = Registry(
    people=[Person("Marco Rossi", aliases=["Marco", "Rossi"], handles=["@marcor"])],
    projects=[Project("Other Show", code="OS1")],
    ids=["123456789"],
)
masked, session = pseudonymize("Marco confirmed Other Show", registry)
# masked.text == "[person 1] confirmed [project 1]"
leaks = session.leaks(reply)      # raw registry names in the reply
restored = session.restore(reply) # raises Unresolved if a stray placeholder is left
```

Matching is by whole word and ignores case. Names in `allow` stay unmasked.
`restore` walks dataclasses, pydantic models, dicts, lists and tuples, accepts
placeholder variants such as `[PERSON 1 ]` or `person 1`, and restores only
placeholders the session issued. Any other placeholder-shaped token raises
`Unresolved` and nothing is restored.

### Masking options and presets

What a task hides is set per task with `MaskOptions`, passed as
`pseudonymize(text, registry, allow, options)` (or `Session(registry, allow,
options)`). It takes a `MaskOptions` or a preset name.

| Option | Meaning | `names` | `strict` |
| --- | --- | --- | --- |
| `names` | whole registered names, aliases and handles | yes | yes |
| `name_words` | single words of a person's name (3+ letters, not a month name) | no | yes |
| `projects` | Project names and codes | yes | yes |
| `ids` | any ID or link found in the text: Notion and t.me links, `tg://`, UUIDs, `@handles`, runs of 7+ digits | no | yes |
| `id_restore` | an ID comes back as `"original"` text or as `"[id]"` | original | `[id]` |
| `ordinary_words` | `"capitalised"`: an ordinary word that is a name word or Project code is hidden only as written capitalised or in capitals | `hidden` | `capitalised` |
| `skip_trivial` | leave one-character aliases and codes, and Project names under 4 letters or ordinary words | no | yes |

`names` is today's default. `strict` reproduces the notion-reports redactor.
Registered IDs (`Registry.ids`) are hidden in both. `restore` fails closed on a
placeholder it did not issue in both.

## LLM

`t6.llm` runs typed LLM tasks. Pydantic v2 is a base dependency. A `Task` has a prompt tag, an input
model and a pydantic output model. Prompts stay in the app: the task's
`render` function turns the input into chat messages.

```python
from t6.llm import DataPolicy, LLMConfig, Ok, Task, run
from t6.privacy import pseudonymize

summarise = Task("summarise-v1", Notes, Summary, render, repair_quotes=True)
masked, session = pseudonymize(text, registry)
provider = LLMConfig.from_mapping(app_config["llm"]).provider_for("summarise-v1")

result = run(summarise, Notes(masked), provider=provider, session=session)
if isinstance(result, Ok):
    use(result.value)          # a Summary, with real names restored
else:
    log(result.reason)         # policy, timeout, api_error, empty, invalid_json,
                               # schema_violation, unresolved_placeholder, leak
store(result.trace)            # prompt tag, model, tokens, latency in ms, repaired
```

`run` never raises for a bad reply. It strips a Markdown code fence if the
whole reply is wrapped in one, parses the reply as JSON, validates it
against the output model, then runs `session.leaks()` and `session.restore()`.
A raw registry name in the reply fails with `leak`, and a placeholder the
session did not issue fails with `unresolved_placeholder`. Failure details
name field paths, never the names themselves. The trace holds no prompt or
reply text; the app decides where to store it.

Only programmer errors raise. Every text value in the input must be masked
the way the task's policy says (below): a plain `str`, or text masked the
other way, raises `UnmaskedInput` before anything is sent. Masked input
without its `Session` raises `MissingSession`. A `str` enum counts as a fixed
value, not free text.

### Data policy

Each provider declares its retention: `zdr_enforced` (zero data retention is
enforced on every request) or `unknown`. Each task declares a `DataPolicy`:
the minimum retention it accepts and the masking its input must carry.
For a `zdr_enforced` task, `OpenAIChat` on OpenRouter also sends
`provider.zdr: true` with every request; other tasks and other providers are
unchanged. A custom provider's `complete` takes `zdr: bool = False`.

| Masking | Every text value in the input must be | Session |
| --- | --- | --- |
| `pseudonymize` | `t6.privacy.Masked` (names swapped, restored after) | required |
| `withhold` | `t6.privacy.Withheld` (credentials removed, names kept) | not needed |

```python
report = Task("report-v1", Notes, Summary, render)   # default: zdr_enforced + pseudonymize
classify = Task("classify-v1", Ticket, Label, render_ticket, policy=DataPolicy("unknown", "withhold"))
```

If the provider's retention is below the task's minimum, `run` returns
`Failed("policy")` and sends nothing.

### Provider config

Provider choice and retention live in the app's config, not in code. The app
parses its own config file and passes the `llm` section as a mapping:

```yaml
llm:
  default: deepseek
  providers:
    deepseek:
      base_url: https://api.deepseek.com
      model: deepseek-chat
      retention: unknown
      api_key_env: DEEPSEEK_API_KEY
    openrouter:
      base_url: https://openrouter.ai/api/v1
      model: some/model
      retention: zdr_enforced
      api_key_env: OPENROUTER_API_KEY
  tasks:
    report-v1: openrouter
```

`LLMConfig.from_mapping(...)` checks the section and raises `ConfigError` for
a missing `retention`, an unknown provider name or an unknown key. API keys
never go in config: `provider_for(prompt_tag)` reads the key from the
environment variable each provider names. A task with no `tasks` entry uses
`default`.

### JSON replies and retries

A reply wrapped in a code fence is always unwrapped. Quote repair (escaping
double quotes left unescaped inside a string value) is opt-in per task with
`repair_quotes=True`. It runs only when the reply fails to parse, before
validation and restore, and the trace then shows `repaired=True`. There is no
other repair and no model repair call: anything else is `invalid_json`.

On a 429 `run` waits once, for the `Retry-After` the provider sent (one
second if none, `api_error` at once if it asks for more than 60), then tries
again. A second 429 returns `Failed("api_error")`. Callers own every other
retry.

`OpenAIChat` speaks the OpenAI-style `/chat/completions` API that DeepSeek
and OpenRouter serve, and asks for a JSON object reply by default
(`json_mode=False` turns that off).

### Jev: Decisions tasks

Jev runs as a normal typed task on OpenRouter's Decisions API. Its `render`
returns a `DecisionsRequest` instead of chat messages: masked `state`, and one
`Choice` question per candidate keyed `c0`, `c1`, ... The output model is
`CandidateDecisions`: a choice (`match`, `maybe` or `no`) and the three
probabilities per key. `CandidateKeys` stays in the app and maps the keys
back to Notion page IDs, so a page ID never reaches the transport.

```python
from t6.llm import (CandidateDecisions, CandidateKeys, Choice, DecisionsRequest, Ok,
                    OpenRouterDecisions, Task, candidate_key, run)

def render(job: MatchInput) -> DecisionsRequest:
    return DecisionsRequest(
        state={"project": job.project.text, "job_order": job.job_order.text},
        questions={candidate_key(i): Choice(f"Does this Job Order belong to '{name.text}'?", CRITERIA)
                   for i, name in enumerate(job.candidates)},
    )

match = Task("match-v1", MatchInput, CandidateDecisions, render)
keys = CandidateKeys([d.page_id for d in deliverables])   # same order as job.candidates
result = run(match, job, provider=OpenRouterDecisions(api_key=key), session=session)
if isinstance(result, Ok):
    by_page = keys.by_page_id(result.value)               # page ID -> CandidateDecision
```

Use `session.mask(text)` to mask each extra field (project name, Deliverable
names) with the same session, so placeholders stay consistent.

The endpoint is alpha, so the transport checks every reply against the
documented shape: `model`, one `choice` answer per question key and no other
keys, probabilities from 0 to 1 that sum to 1, no extra fields such as a
reason. Anything else fails with `schema_violation`, and the detail names
field paths only. Data policy, masking, `leaks()`, `restore()`, the one 429
wait and the trace work the same as for chat tasks. `OpenRouterDecisions`
declares `retention="unknown"` unless told otherwise, so give a Jev task a
`DataPolicy` it can meet, or declare `zdr_enforced` when the account enforces it.

### Testing

Tests use `t6.llm.testing.FakeProvider`, never a live provider or recorded
replies (they can hold real names). Queue steps in order, then run tasks:

```python
from t6.llm.testing import FakeProvider

fake = FakeProvider("deepseek-chat")
fake.reply('{"headline": "[PERSON 1 ] moved the grade"}', prompt_tokens=40, completion_tokens=8)
fake.reply("not json")
fake.timeout()
fake.api_error("HTTP 503")
fake.rate_limited(retry_after=2)
fake.decisions({"model": "typesafe/jev-1.1", "answers": {...}})  # a raw Decisions body
fake.calls  # the request each call sent, to check only masked text left
```

A fake declares `zdr_enforced` retention; `FakeProvider(retention="unknown")`
tests the policy check. `run(..., sleep=...)` takes a stand-in for
`time.sleep` so 429 tests do not wait. `fake.decisions(body)` runs the body
through the same alpha-shape validation as `OpenRouterDecisions`, so a
malformed body fails with `schema_violation`.

## CLI

```console
$ t6 version
{"version": "0.6.7"}
```

`python -m t6 version` provides the same output. `t6 admin schema-check` is
described under [Schema check](#schema-check). `t6 admin registry migrate` and
`t6 admin registry classify` are described under
[Migrating existing Projects](#migrating-existing-projects); they read the
registry file at `--registry-db` (default `$T6_REGISTRY_DB`). `t6 admin drive import` is described under
[Importing the legacy workbooks](#importing-the-legacy-workbooks). `t6 health`,
`t6 admin backup --into DIR` and `t6 admin restore --from DIR` use the server's database
variables and are described in `docs/deployment.md`.

### Studio commands

These read the Notion token from `$T6_NOTION_TOKEN` and the data source IDs
from `--data-sources FILE` (a JSON object of database key to data source ID,
as for `admin schema-check`). They print JSON (indented) with Notion IDs and never write.
Errors are JSON on stderr with exit status 2.

A Person's kind is one of `staff`, `freelancer`, `client` or `vendor`; `person create`
and `person classify` take any of the four, and `person list` and `person get` return it.
Only staff have the staff role.

`t6 departments --data-sources FILE` lists the live Departments (the
`job_orders` source) in display order with their option IDs. MISC is left out.
`t6 departments` and `t6 person get` still accept `--data-sources FILE`, but
the flag is ignored: the API reads with its own data sources. It will be
removed in a later release.

```console
$ t6 departments --data-sources sources.json
{"departments": [{"id": "cU=L", "name": "TOC"}, {"id": "FeJa", "name": "AE"}, ...]}
```

`t6 person find <name|@handle|alias|telegram id> --data-sources FILE` (the
`contacts` source) uses the shared matching policy. It prints the `match`, or
`null` with `needs_review` and the `candidates` (with scores) for a person to
choose. Fuzzy matching needs `t6[lookup]`.

```console
$ t6 person find @msantos --data-sources sources.json
{
  "match": {"id": "<page id>", "name": "Maria Santos", "telegram_handle": "@msantos", "telegram_user_id": 4242},
  "method": "normalized",
  "score": 1.0,
  "needs_review": false,
  "reason": null,
  "candidates": [{"id": "<page id>", "name": "Maria Santos", "telegram_handle": "@msantos", "telegram_user_id": 4242, "score": 1.0}]
}
```

`person find` takes three more inputs (CLI flag, API field). All three are off by
default, and then the reply is the one above.

- `--suggest` (`suggest`): `candidates` are up to 3 Contacts that score 0.60 or
  more, best first, even when `match` is set by an exact match. `match`, `method`,
  `score`, `needs_review` and `reason` do not change.
- `--prefer-telegram` (`prefer_telegram`): the Contacts that have a Telegram user
  ID are searched first and win equal scores. A match found among them is returned
  without searching the rest. It is a hint only: it sets no threshold and no
  honorific list.
- `--mention NAME`, repeated (`mentions`, at most 50 names): instead of the one
  `mention`. The Contacts are read once and the reply is `{"results": [...]}`, one
  reply of the shape above for each name in input order. A blank name gets its own
  result with no match. Giving both `mention` and `mentions`, neither, or more than
  50 names is refused with rule `input`. `suggest` and `prefer_telegram` apply to
  each name.

Name matching is this one call. Apps do not keep matching code of their own, so a
change to the matching rule happens only here. It differs from the rules the apps
used to keep, and these differences are not bugs. In the preferred set, a tie falls
through to all Contacts here, while reports stopped at the tie. The normalizer here
folds accents and punctuation and drops 16 leading honorifics, while the apps
dropped 3 anywhere in the name.

`person list` takes an optional `registered` filter (API field; there is no CLI
flag). `true` returns only the Contacts that have a T6 ID, `false` only those that
do not, and unset returns all. It also takes an optional `kind` (`staff`,
`freelancer`, `client` or `vendor`): only the Contacts whose registered Person
has that kind, so a Contact with no T6 ID never matches. An unknown kind is an
input error (HTTP 422). The two filters combine with AND. The items do not change. Nothing registers a
Contact made in Notion after the first import, so a Contact with no T6 ID has to be
registered: on the registry host run `t6 admin person migrate --data-sources FILE`
without `--apply` to preview, then apply the preview with `--apply --preview FILE`.

`t6 project resolve <code|name> --data-sources FILE [--registry-db FILE]` (the
`projects` source) resolves a Project Code. A reused code is settled by rotation
when the registry file knows the Projects (`--registry-db`, default
`$T6_REGISTRY_DB`); with no registry file every Project counts as a holder, and
an unreadable file is an error. Text that is not a known code is tried as a name
and always needs review. The result has the same `match`, `method`,
`needs_review`, `reason` and `candidates` fields.

`--in-text` (API input `in_text`, default false) treats `text` as longer text
that holds one Project Code, as `Lookup.project_in_text` does (see Reusable
Project Codes). It never falls back to a name match; `reason` is
`no_code_in_text` or `several_codes_in_text` when the text does not hold exactly
one code. Without it the reply is unchanged.

```console
$ t6 project resolve ALP --data-sources sources.json --registry-db t6-registry.sqlite
{
  "match": {"id": "<page id>", "t6_id": "<T6 Project ID>", "name": "Alpha Show", "project_code": "ALP", "status": "In Progress", "rotation": "in"},
  "method": "exact_id",
  "score": 1.0,
  "needs_review": false,
  "reason": null,
  "candidates": [{"id": "<page id>", "t6_id": "<T6 Project ID>", "name": "Alpha Show", "project_code": "ALP", "status": "In Progress", "rotation": "in", "score": 1.0}]
}
```

`t6 standing-cc` prints `{"standing_cc": "<Contact page ID>"}` from
`$T6_STANDING_CC` (`null` when unset). It needs no Notion access.

```console
$ T6_STANDING_CC=<Contact page ID> t6 standing-cc
{"standing_cc": "<Contact page ID>"}
```

### Job Order commands

`t6 admin jo migrate --data-sources FILE` previews the existing Job Orders.
Save its JSON and apply it with `--apply --preview FILE`. It binds by page ID
or an existing Telegram Key, never by name. Conflicts and unresolved Project or
Person references appear in `review`; a repeated apply keeps the IDs and
bindings. A uniquely matched replacement becomes active when the old active
page is absent from the migration snapshot; the old binding remains legacy.
Migrate Projects and People first when their relations are available.

`t6 jo get <t6j ID|page id|telegram key> --data-sources FILE` returns the
Job Order, its T6 ID, active page, assignee Person and append-only notes. A
Telegram Key is `<chat_id>:<message_id>`; put a negative chat ID after `--`.
For an unbound legacy page, get still returns its existing Notion fields.

`t6 jo raise --file jo.json --data-sources FILE` previews a new Job Order.
The JSON accepts the previous create fields, including
`project_id` (a registered T6 Project ID or its Notion page), Contact page IDs
in `assigned_ids`, and `source_text`. Dates without a time zone are Manila;
the Department is checked against Notion's options, standing cc is added and
Watermark defaults to `none`. Confirm with `--confirm TOKEN` and the preview's
`--idempotency-key KEY`. The accepted result contains the `t6j_` ID and a null
`page_id` while Notion creation is pending; use `jo get` or replay the same
confirmed request with a fresh preview after sync to obtain the page ID.

Over the API, `jo raise` takes the fields as a JSON object in `fields` instead
of a file; give exactly one of `file` and `fields` (rule `input` otherwise). A
`fields` call uploads nothing and sends no data sources: the server checks the
fields against its own `job_orders` data source. The two paths share the same
checks, but the `fields` path adds no Watermark, so a caller that sends none
leaves the property unset. The `--file` path still defaults it to `none`.

The call does not wait for the worker. To get the page ID, call `jo get` with
the returned `jo_id` until `job_order.page_id` is set (the worker makes the page
a few seconds after the confirm). To recover after a crash with no `jo_id`,
call `jo find` with the Telegram Key, or with `chat_id` and `message_id`.

Over the API an app sends what a message says and the API applies every rule.
Give `message` beside `fields` (no `file`): `header_text` and `deadline_text` as
posted (either may be empty), `posted_at` (with its time zone), `chat_id` and
`message_id` (both required, else rule `input`), `assignees` (T6 Person IDs),
`project` (a T6 Project ID or page ID, or empty with `no_project`), and
optionally `department_text` (used only when the header has no Department) and
`number_text` (used only when the header does not parse). The API builds the
Name, the JO Number, the Department (the alias map, then the live options), the
Deadline (with "end of shift" read from the End of Shift of each assignee's
Contact), the Telegram Key and the duplicate key (`<Project page ID>|<number>`).
`fields` then carries only what the caller decides (Status, relations, text
fields, Watchdog Notes, body); Name, `number`, Department, Deadline or Telegram
Key in `fields` with a `message` is refused (rule `input`). A header that does
not parse is not a refusal: the Name is the header text and the number comes
from `number_text`. An unknown Department is not a refusal either: none is
written and the reply has a `department_findings` entry. The preview `plan` (and
the confirm result) return `name`, `jo_number`, `department`, `deadline`,
`deadline_source` (`stated`, `shift`, `fallback`, `blank`), `deadline_findings`
(code and params), `department_findings`, `telegram_key` and `duplicate_key`;
the confirm writes exactly these. `jo_id` is `null` in the preview and set at
confirm. `T6_DEPARTMENT_ALIASES` (a JSON object) is merged over the default
alias map `{"TO": "TOC"}`.

A Job Order with no Project is raised only on purpose: send `no_project: true`
(CLI `--no-project true`) with an empty or absent `project_id`. Nothing is
guessed, the page is made with the Projects relation empty, and it keeps the
Status the caller sends. It stays listed as an exception until a coordinator
attaches the Project on the Notion page. `no_project` with a `project_id` is
refused (rule `input`), and an empty `project_id` without the flag is refused
(rule `project`). `assign`, `note`, `submit` and the other Job Order verbs
refuse a Job Order with no Project (rule `project`) until one is attached.
`jo list --no-project true` returns only those Job Orders, oldest first, with an
empty `project_id`. It reads Notion, so a Job Order shows only after the worker
has made its page and leaves the list when the Project is attached. It cannot
be combined with `project` (rule `input`).

`t6 jo assign JO PERSON`, `t6 jo reassign JO PERSON --reason TEXT`, and
`t6 jo note JO TEXT` follow the same preview/confirm flow. PERSON is a `t6n_`
ID. The local account is the actor. Notes and audit are append-only and each
write appears in Project Activity. A repeat with the same idempotency key and
a fresh preview returns the original result.

The main worker needs `T6_NOTION_JOB_ORDERS_DATA_SOURCE` and access to a Job
Orders data source with `T6 ID` and `T6 Notes` rich-text properties. T6 Notes
shows the append-only T6 notes; the existing human-edited JO Notes is preserved. A create whose outcome is
unknown is checked by T6 ID before recovery; if no unique page can be found,
the sync fails for review rather than creating another page.

`t6 jo format --file notes.txt [--date YYYY-MM-DD]` needs no Notion. The file
is a draft: a header line, `Label: value` lines, then free text. It prints
`{"text", "findings"}`: the header rewritten as `<PROJECT> - <DEPT> - JO #<N>`,
the date line (`June 22 / Monday`, computed from `--date`, default today in
Manila), the fields in order with your labels kept, Watermark defaulted to
"none", Status and Pending blank. Anything it cannot settle is a finding
(`header_unparsed`, `missing_title`, `missing_task`, `missing_purpose`,
`missing_assigned`, `no_deliverable_code`, `link_settings_missing`,
`mfg_shuttle_out_of_range`), never a guess. Deadlines are left as written.

```console
$ t6 jo format --file notes.txt --date 2026-06-22
{"text": "ORC - TOC - JO #62\nJune 22 / Monday\n\nTask: ...", "findings": [{"code": "missing_title", "params": {}}]}
```

`t6 deadline <phrase> [--assignees CONTACT_ID ...] [--posted ISO]` resolves a
posted deadline in Manila time with the studio's rules. `--assignees` (Contact
page IDs, read from Notion) gives the end of shift for "end of shift" and
"asap"; `--posted` defaults to now. An unreadable phrase is a finding.

```console
$ t6 deadline "Nov 13 5pm" --posted 2026-11-12T09:00:00+08:00
{"deadline": "2026-11-13T17:00:00+08:00", "display": "Nov 13 / Fri (17:00)", "source": "stated", "findings": []}
```

### Check an app follows the consumer contract

`t6 admin consumer-check PATH` scans Python and common configuration files for
Notion and LLM adapter imports, any other `t6` import, credential names, and
direct service hosts ([ADR 0002](docs/adr/0002-consumer-contract.md)). The credential names are
`NOTION_API_KEY`, `NOTION_TOKEN`, `T6_NOTION_API_KEY` and `T6_NOTION_TOKEN`
(not names that only start with one, such as `NOTION_TOKEN_FILE`), the LLM
and Google keys such as `OPENAI_API_KEY`, `OPENROUTER_API_KEY`,
`DEEPSEEK_API_KEY` and `GOOGLE_APPLICATION_CREDENTIALS`. The host rule reads
every string constant, so a provider URL in test data (a message body that holds
a Frame.io share link) is a finding too; record it in the allow-list as a
`direct-http` exception for that file. An app may import only `t6.client` and
`t6.verbs` (and their submodules; `from t6 import client` is fine). Any other
import from the `t6` package, in any `.py` file including tests, is a
`t6-import` finding that names the file, the line and the module, for example
`imports t6.domain.deadlines`; the rules run in the API, so the app calls it
instead. Python docstrings and
virtualenv or cache directories are skipped. The command prints JSON and exits
1 for findings, or 2 if the path does not exist.

A temporary exception can be recorded in `.t6-consumer-allowlist.json` at the
app root. Each entry names one file and rule, and expires at the end of the
specified local date. All entries are printed on every run; expired or invalid
entries fail the check. For example:

```json
[
  {"name": "migration issue 123", "file": "app/legacy.py", "rule": "adapter-import", "expires": "2027-01-31"}
]
```

Exceptions match an exact path relative to the app root and a finding rule
(`adapter-import`, `credential`, or `direct-http`); keep them narrow and remove
them after migration. A `t6-import` entry is refused as invalid: that rule has
no exception. This is a static check and cannot detect dynamically
constructed names or calls.

To add it to an app's CI, install t6 at the pinned release tag and run it on the
app's source:

```yaml
- run: pip install 't6 @ git+https://github.com/mclbautista/t6-core.git@v0.6.7'
- run: t6 admin consumer-check .
```

## Container image

One image, `ghcr.io/mclbautista/t6`, runs every role. It installs the `server`,
`worker`, `notion`, `privacy`, `llm` and `frameio` extras, runs as a non-root
user, and keeps the queue and Project registry databases in the `/data` volume. The command picks the
role; the default is the server.

```sh
docker build -t t6:dev .                      # linux/amd64 hosts only
scripts/image-smoke-test.sh t6:dev            # `t6 version`, then the server answers
docker run --rm t6:dev python -m t6.server    # the server (VPS)
docker run --rm t6:dev python -m t6.worker    # a worker (Mac mini)
```

Pull requests build the image and run the smoke test; nothing is pushed.

**Release.** Set `version` in `pyproject.toml` (and `uv.lock`), merge, then push
the matching tag:

```sh
git tag v0.6.7 && git push origin v0.6.7
```

The `Image` workflow fails unless the tag equals the pyproject version. It
publishes `X.Y.Z` and `sha-<short>` for linux/amd64, never `latest`, and prints
the digest in the run summary. t6-netops pins `ghcr.io/mclbautista/t6:X.Y.Z@sha256:...`.
After the first publish, set the package's visibility to public in GitHub
package settings so hosts can pull without credentials.

## This repository is public

Anything pushed here is visible to everyone at once, and Docker images install
`t6` from it with no GitHub token. Never commit:

- passwords, API keys, tokens or private keys, even in a test or a comment
- a `.env` file (`.env.example` with empty values is fine)
- a real `config.yaml` (commit `config.example.yaml` instead)
- databases (`*.db`, `*.sqlite`), anything in a `data/` folder, or token store
  files such as `token.json`
- real client, show or person names, or any client data, in code, tests,
  fixtures or docs; use made-up names

If a secret is ever pushed, rotate it straight away. Deleting the commit is not
enough, because the old value is already public.

### Safety checks

The `Public repo` workflow runs on every push and pull request. It fails when
gitleaks finds a likely secret in the new commits, when a tracked file has a
forbidden path, or when a tracked line contains a name from the denylist. The
denylist lives only in the `T6_NAMES_DENYLIST` Actions secret (one name per
line), and the check prints the file and line, never the name. The same job
installs `t6[notion]` from this repo with no credentials.

Run the same checks locally before pushing (needs
[gitleaks](https://github.com/gitleaks/gitleaks#installing) on your `PATH`):

```sh
T6_NAMES_DENYLIST="$(cat ~/.t6-names)" python3 scripts/public_repo_safety.py
```

Add `--full-history` to scan every commit, or `--skip-secrets` to run only the
path and names checks. Other public T6 repositories turn the checks on by
calling the reusable workflow; see the header of
`.github/workflows/public-repo-safety.yml`.

The `Privacy contract` CI job runs the privacy and LLM boundary tests and
requires the names check on pushes and pull requests from this repository. The
check fails if the Actions secret is empty. Fork pull requests skip the denylist
scan and print a notice; this repository does not accept fork pull requests.
Configure the job as a required status check in branch protection. Its denylist
is written from the Actions secret to a temporary file outside the checkout.
Never commit the denylist; keep local copies outside the repository, for example:

```sh
T6_NAMES_DENYLIST_FILE=~/.t6-names python3 scripts/public_repo_safety.py --skip-secrets
```

The names check covers all tracked text files, including case files. It warns
and skips locally when no denylist is configured, but fails in CI when required
and missing. JSON strings are checked after decoding as well as in raw text.
Real cases also require masking and human review before entering a repository.

## Development

This is a single-package uv workspace. Install uv, then run:

```sh
uv sync --locked
uv run --locked mypy
uv run --locked lint-imports
uv run --locked deptry src
uv run --locked python -m unittest discover -s tests -v
uv build
```

The tests exercise the public CLI and build a wheel, then install the base
package and the Notion extra into separate clean environments without an
index. `lint-imports` enforces the layer contracts in `pyproject.toml` (`t6.domain` imports no other t6 package; `t6.llm`, `t6.lookup` and `t6.privacy` stay clear of Notion, Frame.io and the worker), and `deptry` fails when imports and the declared extras disagree. CI runs these checks on push and pull request for Python 3.11–3.14.

## Resolve

`t6.resolve` reads the DaVinci Resolve PostgreSQL project server. Install it
with `pip install 't6[resolve]'`.

- `ProjectServer(db, database, schema_for(version))` lists projects with
  database, folder path, name and internal ID. The layout (`SM_Project` and
  `SM_ProjectFolder`) was checked against the live server on 2026-11-16 and is
  marked verified; an unverified schema is still rejected unless
  `allow_unverified_schema=True` is passed.
  Roles detected as writable are refused (`ReadOnlyViolation`). Connect with
  `t6.resolve.postgres.PostgresDatabase(dsn)`, which also sets the session
  read-only.
- A project's External ID is `<database>/<folder path>/<project name>`; a
  project outside any folder has no folder segment. `ResolveProject.ref` is the
  ExternalRef (system `resolve`, kind `project`, Internal ID the server's
  project ID).
- `map_projects(projects, links)` matches projects to Media locations (kind
  `resolve-project`, role `working`). `unmapped` holds projects with no
  location; `orphan_links` holds locations whose project is no longer on the
  server.
- `schema_for(version)` matches on the major of a Resolve version (the text
  before the first `.`); 17, 18, 19 and 20 give the verified layout, any other
  major raises `KeyError`. Tests use a synthetic SQLite fixture in
  `tests/fixtures/resolve/`.

The table layout in `t6.resolve.schema` was checked read-only against the live
server (PostgreSQL 13.4, server app 20) on 2026-11-16.

### CLI rename table

Release 0.5.0 removed the old commands. Each now fails with exit status 2 and
an error naming the new command, for example `t6 jo create was renamed to t6 jo
raise`; the error stays for one more release.

| Previous command | Current command |
| --- | --- |
| `jo create` | `jo raise` |
| `jo get`, `jo format` | unchanged |
| `contacts find` | `person find` |
| `projects resolve` | `project resolve` |
| `cameras get` | `camera get` |
| `cameras update` | `camera enrich` |
| `departments`, `standing-cc`, `deadline` | unchanged |
| `migrate-projects` | `admin registry migrate` |
| `classify-projects` | `admin registry classify` |
| `schema-check`, `check` | `admin schema-check` |
| `schema-snapshot` | `admin snapshot` |
| `backup`, `restore` | `admin backup`, `admin restore` |
| `check-consumer` | `admin consumer-check` |
| `drives-import-dry-run` | `admin drive import` |
| `version`, `commands --json`, `health` | unchanged |

Project hold and resume use a preview followed by confirmation. Configure
`T6_REGISTRY_DB` and `T6_PREVIEW_SIGNING_KEY` for the local CLI. For example,
`t6 project hold PROJECT --reason "waiting"` returns a token and an
`idempotency_key` in its plan. Repeat the same command with `--confirm TOKEN`
and `--idempotency-key KEY` to confirm it; resume uses `t6 project resume
PROJECT` in the same way. The local operating-system account is recorded as
the actor. Tokens expire after 15 minutes and are single-use. Keep the same
idempotency key for retries and obtain a fresh preview token for each attempt.

Hold and resume queue Project Status projection intents. The current sync
worker only projects identity fields, so these intents remain pending until a
Status-capable worker is provided.

## Transcription

Install `t6[transcribe]` for the ElevenLabs adapter. Construct an ElevenLabs
client with the caller's API key and pass it to
`transcribe(wav_path, client)` from `t6.transcribe.adapter`. The result is the
SDK's word-level JSON as a dictionary. `language="en"` overrides the default
Filipino language hint; English remains the secondary hint.

Transcribe worker variables (`python -m t6.worker` with `T6_WORKER_QUEUE=transcribe`):

| Variable | Read by | Meaning |
| --- | --- | --- |
| `T6_TRANSCRIBE_SHARES` | the transcribe worker only | Path of a JSON file on the worker's host mapping each NAS share name to its mount point there, for example `{"Audio": "/Volumes/Audio"}`. Required, no default; the worker exits 2 when it is unset, unreadable, not a JSON object of strings, empty, or has a relative mount point. |
| `ELEVENLABS_API_KEY` | the transcribe worker only | The ElevenLabs key. No other process holds it. |
| `T6_TRANSCRIBE_RATE` | the API server (`python -m t6.server`) | Price per audio hour used for previews, default `0.4` USD. The worker never reads it. |

### Floor reads: bangko, hand-to-mouth, sabit

`t6 deliverable bangko`, `t6 deliverable hand-to-mouth` and `t6 deliverable sabit`
are read verbs (role reader, read-only, so they may appear as MCP tools). Each takes
an optional `--project` (T6 ID, Notion page ID or code); without it the read is
studio-wide. Nothing is stored. Dates are Asia/Manila days. A Deliverable is ready on
the earlier of its `delivered` Milestone date and the day its Package became kasado
(every blocking checklist item ticked, Package not rejected). Deliverables without
an air date are skipped.

| Variable | Read by | Meaning |
| --- | --- | --- |
| `T6_BANGKO_DAYS` | the API server | Bangko: ready at least this many days before air. Default `3`. |
| `T6_HAND_TO_MOUTH_DAYS` | the API server | Hand-to-mouth: ready 0 to this many days before air. Default `1`. |

Both must be whole numbers of days, zero or more; otherwise the read is Refused
(`config`). Sabit lists Deliverables with a recorded miss (`deliverable miss`). A
date moved with a reason (`deliverable delay`) is delayed and is not sabit.

A `nas-path` Media location is read as `/Volumes/<share name>/<folders>`. The worker
maps it to `<mount point>/<folders>` and resolves symlinks. When the share is not in
the mapping, the ref has another form, or the resolved folder is outside the mount
point (a `..` escape or a symlink that leaves it), the job is refused with rule
`outside_shares` before anything is read or written. This covers `transcript.preview`,
`transcript.run`, `transcript.speakers.preview`, `transcript.speakers.run` and the
translate jobs.

`render_srt(transcript, offset=0, fps=24)` from `t6.transcribe` needs no SDK.
Offsets are seconds or non-drop-frame `HH:MM:SS:FF` timecodes; set `fps` to
the timeline's frame rate. Words with missing timestamps are skipped.

`parse_speaker_map(csv_text)` reads `source,speaker,name` CSV text. A blank
source applies to every clip; an exact clip-relative source overrides it.
Pass the resulting map and clip path as `speaker_map` and `source` to
`render_srt` or `render_docx`. Unmapped speakers keep their labels. SRT retains
its original unlabeled output when no map is supplied. `render_docx` returns
validated DOCX bytes, with one paragraph per speaker turn and the same offset
and frame-rate options as SRT. Neither renderer calls ElevenLabs.

`t6 transcript translate <transcript> [--confirm TOKEN]` writes `.en.srt` and
`.en.docx` beside a Transcript's outputs under `_transcripts`, translating cue by
cue through the API's `llm run`. The worker reaches the LLM only through the API route
`POST /v1/transcribe/llm`, which accepts the transcribe worker alone and only with the live
lease of a translate run job. It is off unless the API server has
`T6_TRANSCRIPT_TRANSLATE=true`; until then it returns Refused (`translate_disabled`).
The first call queues a preview (clips, cue count, estimated tokens); the same call
with the signed single-use `--confirm` token translates and records a `translated`
history entry and an Activity entry. A failed translate run records a `failed` history
entry. The transcribe worker holds no provider
credentials; the API masks registered names, Project codes and credentials.
Synthetic eval cases and code graders are in `t6.transcribe.translate_evals`.
A recorded live run (#213) must pass before the task is released.

### Studio verb HTTP API

The full server exposes the studio API on its tailnet listener. Set
`T6_API_SECRETS` to a JSON object mapping each distinct bearer secret to one
Person ID (for example `t6n_...`) or one of `gateway`, `worker`, `reports`,
`watchdog`, `munki`, `hermes`, and `Sixto (auto)`. `Sixto (auto)` is the actor of
automatic Concern changes: it may run Concern verbs and nothing else, and its
reach is fixed in code, not in `T6_API_SERVICE_PERMISSIONS`. Keep this
configuration secret. Set
`T6_PREVIEW_SIGNING_KEY` for previewed writes. Send `Authorization: Bearer
<secret>` and use `GET /v1/commands` for the catalogue. Invoke a verb with
`POST /v1/<noun>/<verb>` and its JSON input. `departments` is at
`POST /v1/departments`. Supply the preview token as `confirm` and preserve the
`idempotency_key` from its plan (or use the `Idempotency-Key` header). Only
the gateway may send `X-Telegram-User-Id` to act as the active Person bound
to that ID. Named services have the `reader` role by default. To grant additional roles,
set `T6_API_SERVICE_PERMISSIONS` to a JSON object mapping service names to role
lists, for example `{"worker":["operator"],"reports":["coordinator"]}`.
Valid additional roles are `operator`, `coordinator`, `bookings`, `drives`,
`llm`, `unmasked`, `fleet` and `admin`; admin includes operator. `unmasked` lets a
caller read real client names (read replies are otherwise masked) and grants
nothing else; the `mcp` service stays masked. `fleet` allows `machine sync` and
nothing else (`machine list` needs only `reader`; for example
`{"munki":["fleet"]}`). Grants apply to every secret bound to
that service. Gateway requests carrying `X-Telegram-User-Id` use the resolved
Person's permissions instead of the gateway's grants. The legacy `camera enrich`
endpoint returns `refused` because it does not yet support signed preview and
confirmation. `GET /health` reports the
package version, database schema version and job counts by state.

For gateway tap delivery, bind its secret to `gateway` in `T6_API_SECRETS` and
set the gateway's `t6core.push_url` to `http://<t6-api-lan-address>:8081/tap`.
Each push must include `X-Telegram-User-Id` matching the payload's `from.id`.
The API listener must bind to a reachable LAN address; edge mode does not serve
`/tap` on the VPS. See [operator actions](docs/operator-actions.md#rollout).

### LLM tasks on the API

Grant the `llm` permission explicitly through `T6_API_SERVICE_PERMISSIONS` (or
Person permissions). Set `T6_LLM_CONFIG` to a JSON file on the API host. It
contains `llm` in the format accepted by `t6.llm.LLMConfig`, including a
`default` provider, and a `privacy_registry` object with `people` (objects with
`name`, optional `aliases` and `handles`), `projects` (objects with `name` and
optional `code`) and `ids`. Keep this registry current: masking can only
recognize registered names. Provider keys are read from the provider's named
environment variable, never from the file. The configured providers and their
models are the allowlist. `require_zdr` defaults to true; `max_bytes` defaults
to 100000 for the combined UTF-8 template, input, examples and canonical schema.
`provider_min_retention` is optional: a map from a provider name in `llm` to
`"unknown"` or `"zdr_enforced"`. It sets the retention floor for that provider
only; other providers keep the `require_zdr` rule, and a caller's
`require_zdr: true` still tightens it. A provider with retention `unknown` is
used only when listed here as `"unknown"`. A name that is not a configured
provider, or any other value, stops the server at start-up. Each audit record
also holds the `provider` used and whether `zdr` was required.
Install the `server` extra for JSON Schema validation.

POST `/v1/llm/run` with `task_name`, `prompt_template`, `output_schema`,
`input_text`, `masking_preset` (`names` or `strict`), and optional `model_hint`.
The template must contain exactly one literal `{input}` marker. The response
contains the restored output, provider, model, token counts and SHA-256 hashes:
the template's UTF-8 bytes, and the schema serialized as UTF-8 JSON with sorted
keys, no extra whitespace and non-ASCII characters preserved. Only local JSON
Schema references are supported. Audit records contain the actor, outcome,
provider and `zdr` flag, not the task text or output.

`examples` is an optional list of `{input_text, output}` pairs for few-shot
prompting. Each becomes a user turn (the template with the masked example text)
and an assistant turn (the masked JSON output), sent before the real user turn
and masked with the same placeholders. An example `output` that does not match
`output_schema` is refused with rule `schema`.

A refusal with rule `provider`, `policy` or `masking` carries `failure`, one of
`timeout`, `api_error`, `leak` or `policy`; for a 429 it also carries
`retry_after` in seconds when the provider sent one. Other verbs' refusals have
neither key. When the reply is not usable the call is `done` with
`output_valid: false` and `failure` set to `empty`, `invalid_json`,
`schema_violation` or `unresolved_placeholder`. The server never retries.

Four more optional inputs serve reports. The leak check on the masked input and
on the reply follows `masking_preset`, so `strict` no longer refuses a plain-word
Project name it leaves visible on purpose. `allow_projects` lists Projects (by
name or code, as in the privacy registry) whose names stay readable; any other
entry, or a person, is refused with rule `allow`, and the audit record holds
only the count. With `repair_quotes: true`, a reply that is not valid JSON gets
one quote repair, and `repaired` says so. `restore: "per_field"` restores every
string it can: a string with a placeholder the call did not issue stays as
written and its path (for example `changes[2].text`) is listed in `unresolved`,
and `output_valid` stays true. `masked_output` then holds the reply as the model
wrote it, placeholders only. With `restore: "whole"` (the default) one such
placeholder makes the call `output_valid: false`.

POST `/v1/llm/decide` (`llm decide`, role `llm`) asks the Decisions provider about
candidates. Send `task_name`, `state` (text values), `question` (exactly one
`{candidate}` marker), `criteria` (`match`, `maybe` and `no`) and `candidates`.
The server masks `state` values and candidate names with one Session, keys the
candidates `c0`, `c1` and so on, and returns `answers` in candidate order, each
with `index`, `choice` and the `match`, `maybe` and `no` probabilities, plus
`model`, `provider`, `tokens` and `zdr`. A registered name in `question` or
`criteria` is refused with rule `masking`. The caller decides what is sure
enough. Without a top-level `decisions` key in the config file (`url`, `model`,
`retention`, `api_key_env`) the verb is refused with rule `config`. Its retention
floor is the `provider_min_retention` entry named `decisions`. Failures carry
`failure` as for `llm run`; a reply off the Decisions shape is `schema_violation`.

POST `/v1/llm/filter` (`llm filter`, role `llm`) asks the Decisions provider whether
one message passes a named rule. Send `rule`, `text` and optionally `reply_to_text`
(the message it answers). The question and the `match`, `maybe` and `no` criteria
belong to the rule, defined in t6-core (`t6.domain.message_filter`); the first rule,
`job_order`, asks whether the message is a Job Order and sits with the Job Order
rules. `text` and `reply_to_text` are masked in one Session and checked for leaks, as
in `llm decide`; the Decisions provider, its retention floor, the size limit and the
`failure` classes are the same. An unknown `rule` is refused with rule `input`. The
reply holds `verdict` (`pass`, `drop` or `unsure`), `probabilities` (`match`, `maybe`,
`no`), `rule`, `model`, `provider`, `tokens` and `zdr`. `pass` means the probability of
`match` is at least 0.5; `drop` means the probability of `no` is at least 0.9; anything
else is `unsure`. A top-level `filters` key in the config file overrides them for a
rule, for example `"filters": {"job_order": {"pass": 0.5, "drop": 0.9}}`; an unknown
rule or a value outside 0 to 1 stops the server at start. The audit row holds the
outcome, rule, verdict, provider and `zdr`, never text or probabilities. The verdict is
advice: the caller decides what a `drop` does. A caller must treat a refusal as
"go on" (fail open).

### Calling the t6 API

Install `t6[client]` and set `T6_API_URL` and `T6_API_SECRET`. The client
uses the same typed input models as the verb registry:

```python
from t6.client import Client, Unavailable
from t6.verbs import ProjectHoldInput, Preview

client = Client()
request = ProjectHoldInput(project="t6p_...", reason="Waiting", idempotency_key="outbox-123")
preview = client.project_hold(request)
if isinstance(preview, Preview):
    # Show the plan to a person and obtain their confirmation first.
    request.confirm = preview.token
    result = client.project_hold(request)
```

Handle all five outcomes and `Unavailable`. Before sending a write, persist its
input and a unique idempotency key in the app's own ledger or outbox. On an
unavailable response or a lost confirmation response, retain the same key and
input. Obtain a fresh preview, have it confirmed, and retry with its token and
the original key. Confirmation tokens are single-use; the key ensures an
already completed write is returned without applying it twice. `Unavailable`
contains the key sent with the request. HTTP errors such as authentication or
invalid input raise `APIError`. Only the gateway may set `telegram_user_id` on
the client to act on behalf of a Telegram user. The gateway can deliver `/tap`
with `client.tap(payload, idempotency_key=key)`; use `client.llm_run` for LLM tasks.

`camera enrich` is temporarily local-only: its legacy write lacks the API's
signed preview and confirmation contract. It uses the admin's own credentials
until it gains that contract. `jo raise` uses the API for signed preview and
confirmation. The client sends the contents of data-source configuration and draft files to the API. Project
resolution uses the server's registry, not a caller-supplied database file.

### Package handoffs

`package prepare <deliverable> <kind> --to <destination> --recipient <person-id>
--data-sources <file>` snapshots the matching `package_specs` Notion data source.
Use `--client-spec` to select a client-specific spec and `--note` for context.
The authenticated Person is the sender. Check an item with `package check
<package-id> <item>`, then use `package handoff <package-id>`. Missing advisory
items remain visible in Activity and `package list --missing`; missing blocking
items prevent handoff. The recipient uses `package accept <package-id>` or
`package reject <package-id> --reason <reason>`. These writes use the usual
preview, confirmation token, and idempotency key. `package get <package-id>`
shows the snapshot, checklist, and derived `kasado` readiness.

## Concern settlement and Project flags

Concerns may link to a Job Order, Approval, to a Deliverable and an
awaited Milestone kind, or to an awaited Project date (`settles_date`:
`playdate`, `submission_deadline` or `archival_deadline`). Recording the
matching fact, or observing the date filled in Notion, settles the Concern and
adds an Activity entry attributed to `Sixto (auto)`; a manual reopen retains
its original age and takes precedence over later automatic settlement.
Date sanity compares Submission Deadline with Playdate and Archival Deadline
with Submission Deadline. Bound Project page observations reconcile those
rule-made Concerns when a single bound coordinator and both dates are known.

A Concern is shared information: a waiting-on record that also holds the text
of the brief. It carries `text` (the line the brief shows; it defaults to the
`anchor`), `source` and `closed_at` (set by every path that closes it, empty
while it is open). `source` is `person` (`concern raise`, the default),
`pm_notes_scan`, `date_rule`, `flag_rule`, `pm_notes_read` or `leave`; a bad
value is refused with rule `source`. A Concern whose source is not `person` may
be raised with no `owner`, and any Concern may have an empty `waiting_on`: the
"named in the line" rule applies only when `waiting_on` is given, and a
`person` raise still needs an owner. A Concern that is not closed is unique by
Project and `text` (whitespace collapsed, case folded); a second one is refused
with rule `duplicate`. `concern get` returns the new fields. `concern list
--open` returns every Concern that is not closed (`open`, `quiet`, `expired`),
oldest first, with `age_seconds` and `chronic` (open longer than
`CHRONIC_DAYS`, 28); pass exactly one of `--raise-at-psr` and `--open`. `text`
is masked like `waiting_on` and `anchor` for a caller without real-name access;
reading it needs the `unmasked` permission.

`concern observe` is how an app hands over the findings of one run (role
`coordinator`; the reports service also needs `unmasked` to read the reply
unmasked). `t6 concern observe --file findings.json --idempotency-key KEY
[--dry-run]` reads `{"run_date", "findings", "unavailable"}`; a finding is
`{project, text, source, owner, waiting_on}` with `source` `flag_rule`,
`pm_notes_read` or `leave`, and `unavailable` lists the `{project, source}` pairs
that could not be read. The call is one step (no preview) and needs an
`idempotency_key`; the same key and input return the first reply and write
nothing twice. At most 500 findings and 100 unavailable pairs. For each finding
the API decides: new (no Concern that is not closed has the text in the Project:
a Concern opens with `opened_at` the run date), known (evidence is added, an
open or quiet or expired Concern is open again, `opened_at` is kept), or, for a
Concern the run did not find, not seen (nothing changes; the silence timers go
on). The same text after a Concern was closed is a new Concern with a new age.
A Concern of source `person`, `pm_notes_scan` or `date_rule` is never created,
aged or closed by the run; it is listed in `open`. A finding whose `owner` is not
an active coordinator of the Project is skipped, a finding with no owner is not.
With `T6_CONCERN_OBSERVE_CLOSES` on, a not-seen Concern whose pair was not
`unavailable` is closed, unless a person decided it. The reply holds, for each
Project, `open` (`id`, `text`, `source`, `opened_on`, `age_days`, `weeks`,
`chronic`, `state`, `seen_this_run`) and `closed_since_last_run`, and the counts
`new`, `known`, `not_seen`, `unavailable`, `skipped`, `projects_with_both` and
`closed`. `--dry-run` computes the same reply and writes nothing. Every real run
writes one `concern_observe` audit row and one log line holding the counts, never
text, names or IDs; the PM Notes scan writes a line of its counts too
(`projects`, `raised`, `continued`, `announced`).

`concern counts` (role `reader`, read only; `t6 concern counts --since DATE
--until DATE [--project P]`) answers how many Concerns each PSR raised and how
they age and close. For each `run_date` in range, oldest first, it returns
`listed` (the Concerns `concern observe` returned in `open` on that real run),
`sent` (of those, the ones with a `concern record-message` for that date: pass
`brief_date` to it), `new` (opened that day), `closed` (closed since the previous
listed date, up to and including that day), `age_buckets` (`under_2_weeks`,
`2_to_4_weeks`, `over_4_weeks`) and `by_source`. For the whole range it returns
`closed_in_range`, `median_days_open` and `still_open`. The default range is the
last 12 run dates. A reply holds counts only: no text, names or Project names.
`listed` starts at the first real `concern observe` run after the switch; the old
reports store is not imported, so earlier listings are unknown.

`project flags` recomputes date warnings and missing health from the current
Project data, without Concern IDs or ages. It takes `as_of`, `lead_warning_days`
and `stated` (dates the PM Notes give for blank date properties; `null` is a
fuzzy value), and also returns `missing_roles` and `sanity` (the date problems,
as kinds and dates, with `from_notes`). `project list` and `project get` return
`status_key`, `status_group`, `snapshotted`, `missing_roles`, `relevant_dates`,
`health_missing` and `health_rank`, and `project list` filters on `status_group`;
`jo list`, `jo get` and `jo find` return `status_group`, `active` and
`health_flags`. Apps do not evaluate these rules: see `docs/api-read-verbs.md`. Health-vs-evidence and source
conflict are judgments supplied by the reports app. Its stale-milestone
judgment also depends on dated notes and later mentions, which are not stored
as structured milestone expectations in t6; the read cannot infer that flag
from a Deliverable deadline alone.
