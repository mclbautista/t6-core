# t6-core

The shared T6 Python library. This repository was renamed from **t6-notion**
before its first commit. The distribution and import namespace are `t6`.

Requires Python 3.11 or newer. `t6` is not on PyPI: install it from this
repository, pinned to a release tag, with
`pip install 't6 @ git+https://github.com/mclbautista/t6-core.git@v0.3.3'`.
Where this README says `pip install 't6[extra]'`, use the same form with the
extra, for example
`pip install 't6[notion] @ git+https://github.com/mclbautista/t6-core.git@v0.3.3'`.
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
  carry the Project's `key`. Job Orders, Deliverables and External Links still
  hold page IDs; see [Migrating existing Projects](#migrating-existing-projects).
- A Project's human key is its Project Code, which may be blank (`None`).
- `Project.episodes` derives Episodes 1 to `Eps` for the series Project Types
  (`SERIES_PROJECT_TYPES`). Films and other types have no Episodes.
- An ExternalRef (system, kind, external ID, URL, internal ID) mirrors one
  External Links row. System, kind and external ID identify the object; the URL
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
  `/queue/*` (workers and callers), `POST /review/timers` (the Frame.io worker) and `/registry/*` (the
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
| `T6_WORKER_POLL_SECONDS` | optional: sleep after an empty poll, default `5` |
| `T6_NOTION_TOKEN` | main worker: Notion integration token, for the Project registry sync |
| `T6_NOTION_PROJECTS_DATA_SOURCE` | main worker: ID of the Projects data source |
| `T6_REVIEW_CHAT` | main worker: required when the gateway variables are set, the Telegram chat ID that gets the [review message](#frameio) |
| `T6_REVIEW_TOPIC` | main worker: optional, the forum topic ID in that chat. Leave it unset for the General topic: Telegram rejects topic 1 |
| `T6_GATEWAY_URL`, `T6_GATEWAY_SECRET` | Frame.io worker: required, the gateway whose `/notify` sends the sign-in alert |
| `T6_FRAMEIO_ALERT_CHAT` | Frame.io worker: required, the Telegram chat ID that gets the sign-in alert |
| `T6_FRAMEIO_CLIENT_ID`, `T6_FRAMEIO_CLIENT_SECRET` | Frame.io worker: required, the Adobe OAuth Web App credential. Give them to no other process |
| `T6_FRAMEIO_REDIRECT_URI` | Frame.io worker: required, the `https://` address of the server's `/frameio/callback`, exactly as set on the Adobe credential |
| `T6_FRAMEIO_TOKEN_FILE` | Frame.io worker: required, where the token is kept, on a volume only this worker mounts |
| `T6_FRAMEIO_ACCOUNT_ID` | Frame.io worker: optional, the Frame.io account ID; needed only when the sign-in sees more than one account |
| `T6_NOTION_TOKEN`, `T6_NOTION_EXTERNAL_LINKS_DATA_SOURCE` | Frame.io worker: optional, turn on the briefing feed and the External Links rows for live share links |
| `T6_FRAMEIO_READ_BIND`, `T6_FRAMEIO_READ_SECRET` | Frame.io worker: optional, turn on the [read API](#frameio): `host:port` on the LAN or tailnet address (never a public one) and the bearer secret callers send. Both are needed together |

| Variable | Use |
| --- | --- |
| `T6_FRAMEIO_WEBHOOK_SECRET` | Frame.io signing secret |
| `T6_NOTION_WEBHOOK_TOKEN` | Notion `verification_token` |
| `T6_TAP_SECRET` | the gateway's `T6CORE_SECRET` |
| `T6_WORKER_SECRET` | main worker: claims `main`, may submit to `main` and `frameio`, reads Projects and reports Notion sync results |
| `T6_FRAMEIO_WORKER_SECRET` | Frame.io worker: claims `frameio`, may start review timers (`POST /review/timers`); also turns on `/frameio/callback` |
| `T6_FRAMEIO_CALLER_SECRET` | a caller outside the main worker (t6-notion-reports): may submit to `frameio` and read its results, nothing else |
| `T6_DIAGNOSTIC_SECRET` | optional isolated diagnostic queue submit/claim credential; must differ from all other server secrets |
| `T6_REGISTRY_SECRET` | registry caller that may register and read Projects |
| `T6_REGISTRY_READ_SECRET` | registry caller that may only read Projects |
| `T6_QUEUE_DB`, `T6_PUBLIC_BIND`, `T6_TAILNET_BIND` | database path, listener addresses |
| `T6_REGISTRY_DB` | Project registry database path, default `t6-registry.sqlite` |
| `T6_OPERATOR_IDS` | Telegram user IDs allowed to tap operator cards and send `/project`, comma separated; the first receives the cards core starts, and a `/project` card goes to whoever sent it. Unset turns operator actions off |
| `T6_OPERATOR_DB`, `T6_OPERATOR_TTL_SECONDS` | operator action database, default `t6-operator.sqlite`; how long a card stays live, default 7 days |
| `T6_REVIEW_QUIET_SECONDS` | server: how long one person must make no new comment on one Frame.io file before the review message is sent, default 900 (15 minutes); must be a positive number |
| `T6_GATEWAY_URL`, `T6_GATEWAY_SECRET` | main worker: the Sixto gateway and this module's secret there; turn on delivery of operator cards |

Tests use `t6.server.testing.FakeClock`; no test opens a connection to a
provider.

## Consumer contract

Every app uses t6-core for Notion, LLMs, privacy, Frame.io, Resolve and the
fleet. The rules are in
[`docs/adr/0002-consumer-contract.md`](docs/adr/0002-consumer-contract.md).

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
3. **Preview**: `t6 migrate-projects --data-sources sources.json > preview.json`.
   `sources.json` maps `projects` (required) and `job_orders`, `deliverables`,
   `external_links` to data source IDs. The preview lists each distinct page,
   whether it would be registered or is already, how many Job Orders,
   Deliverables, External Links and Episodes point at it, the codes shared by
   several pages and the pages with no code. A reference kind whose data
   source was not given is listed under `references_not_counted`, never shown
   as zero. Drive Assignments are always listed there: they cannot be counted
   per Project yet. Read the preview before the next step.
4. **Apply**: `t6 migrate-projects --data-sources sources.json --apply --preview
   preview.json --actor <name>`. It registers every page that has no Project,
   **unclassified**, with its recorded code, and prints a report. It refuses
   (exit 2) when the pages differ from the preview. Exit 1 means some pages
   could not be registered (for example a code with no letter or digit); fix
   them in Notion and run it again. A rerun keeps every ID it already issued
   and registers only what is new.
5. **Classify**: write a JSON object of T6 ID (or page ID) to `in` or `out`
   and run `t6 classify-projects --decisions decisions.json --actor <name>`.
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
- `ExternalLinks(client, data_source_id, refs)`: `refs_for_project` and
  `links_for_project` take either form and find rows related to any page the
  Project has had. `ensure(project_id=)` relates a new row to the active page.
  Without `refs` a Project is its page ID, as before.
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
rollout order, `t6 health`, `t6 backup` and `t6 restore`, and a verification log
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

### Importing the legacy workbooks

`t6.drives.importer` needs the `drives` extra. It provides import and local
review capability; a real-data import, workbook freeze and cutover require
separate human approval. No such operation is performed by this change.

```python
from t6.drives.importer import import_per_project_workbook

result = import_per_project_workbook(path, store, lookup=lookup, logged_by=Ref(text="workbook import"))
```

- The per-project workbook has one tab per Project: an optional
  `PROJECT NAME: ...` row, a header row starting with `HARD DRIVE LABEL`, then
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
- `t6 drives-import-dry-run WORKBOOK [--log-sheet FILE] [--projects FILE]` runs
  the import into an in-memory store that is thrown away and prints
  `{"imported": n, "review": [...]}`. `WORKBOOK` is the per-project workbook.
  Nothing is written anywhere. `--projects` is a JSON list of
  `{"page_id", "name"}` to resolve Project names against; without it every
  Project is reported as unresolved.

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

The `notion` extra installs `httpx`, the `llm` extra installs `pydantic`, the `lookup` extra installs `rapidfuzz`, the `resolve` extra installs `psycopg` and the `drives` extra installs `openpyxl`.
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
  linked to (`frameio` / `project` External Links row), or none. The feed also
  gives every live share link a `frameio` / `share_link` External Links row
  if it has none, and moves the row's Last Activity forward. It reports the
  row's Active box and never writes it. Running it again creates no
  duplicate. The feed needs `T6_NOTION_TOKEN` and
  `T6_NOTION_EXTERNAL_LINKS_DATA_SOURCE` on the Frame.io worker and the
  `notion` extra; the other reads do not. A feed over many share links can
  take minutes because of Frame.io's rate limit, so give `ask` a long
  `timeout`.
- **Read API.** With `T6_FRAMEIO_READ_BIND` and `T6_FRAMEIO_READ_SECRET` set,
  the Frame.io worker also serves read-only HTTP (`t6.frameio.readapi`), so an
  app such as notion-reports gets Frame.io data with a read secret and no
  Frame.io token. Routes, all `GET` with `Authorization: Bearer <secret>`:
  `/frameio/status`, `/frameio/projects`, `/frameio/projects/<id>/share-links`,
  `/frameio/files/<id>/comments`, `/frameio/files/<id>/approvals` and
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
  same file restarts the full `T6_REVIEW_QUIET_SECONDS` (default 900); other
  people and other files have their own timers. When a timer runs out, the
  main worker runs `frameio.review_done` (`t6.worker.review.ReviewMessage`)
  and sends one Telegram message through the gateway's `/notify` to
  `T6_REVIEW_CHAT` (and `T6_REVIEW_TOPIC` if set) with the file's title, who
  commented, when they started and stopped (Manila time), how many comments they
  made and how many the file has in total, and two links: the newest share
  that is on, not expired and made from the file (its short URL, or "no share
  link"), and the internal Frame.io link for signed-in people. No share
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
blank end is a milestone). `link_timelines(timelines, ExternalLinks(...))`
ties each timeline to its Notion Project through an External Links row
(System `instagantt`, Kind `project`, External ID = the export's Project ID,
or the project name when the export has none). A project with no row, a row
with no Project or a row with several Projects comes back in `unmapped` with
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

### External Links

`t6.notion.external_links.ExternalLinks(client, data_source_id)` reads and
writes the External Links database:

- `refs_for_project(project_id)` returns the Project's rows as ExternalRefs,
  Internal ID included. `links_for_project` also returns the row's page ID,
  Active and Last Activity. With a `ProjectRefs` as the third argument, either
  accepts a T6 ID or any page of the Project.
- `ensure(ref, project_id=...)` creates a row only when no row has the same
  System, Kind and External ID. If one exists, it is returned and nothing is
  written. It never sets Active, because people do.
- `touch(page_id, when)` sets Last Activity and writes nothing else.

The property names and types are not yet checked against the live database
(see the module docstring).

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
`t6 schema-snapshot --data-sources FILE --out FILE` (same file format as
`schema-check`) and regenerate; no code changes. A property whose entry has no
ID yet is addressed by name. The `t6.notion.generated`
module has one model per database, such as `BaysRow` and `JobOrdersRow`. Run
`uv run python scripts/generate_models.py` after the snapshot changes. A test
fails when the module is out of date.

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
`t6.notion.generated`. `get(page_id)`, `find_by_telegram_key(key)` and `list()`
read; `create({...})` writes through the typed write and returns the saved
`JobOrder`, `Rejected` or `Ambiguous` (a body that could not be saved leaves a
page, so it is `Ambiguous` with that page ID). The only Job Orders property
no field carries is `place`.

`status` and `department` are named values (`JobOrderStatus`, `Department` in
`t6.domain`). `t6 schema-snapshot` records each one's Notion option ID in the
snapshot (`named_options`), found by matching the live option name once. After
that an option renamed in Notion is still read and written by ID. A value with
no ID recorded is sent by its label, and a status or department Notion has but
t6-core does not know reads as its plain name. `t6 schema-check` reports a
named value whose option is gone under `named_missing`, as drift.

### Projects, Contacts and Deliverables

`t6.notion.reads` has `ProjectReader`, `ContactReader` (each with `get` and
`list`) and `DeliverableReader.for_project`, returning `t6.domain` entities
addressed by property ID. `Project` carries status, stage, health, playdate,
submission and archival deadlines, PM and tech notes, `has_post_supe`, and the
role relations as page IDs (`editor_ids`, `assistant_editor_ids`,
`online_editor_ids`, `colorist_ids`, `sound_tech_ids`, `post_coordinator_ids`,
`director_ids`, `cinematographer_ids`). `Contact.end_of_shift` is a time of day
parsed from "10:00 PM", "10 PM" or "22:00"; "Flexible / Variable", blank and
any other text are `None`, meaning flexible. `Deliverable` carries
`deliverable_class` and `playdate`.

### Schema check

`t6 schema-check` diffs live Notion against the snapshot. It needs the Notion
extra, a token in `T6_NOTION_TOKEN`, and a JSON file that maps snapshot
database keys to live data source IDs (keep that file out of the repository):

```console
$ cat data-sources.json
{"projects": "<data source id>", "bays": "<data source id>"}
$ t6 schema-check --data-sources data-sources.json
```

The report is JSON. For each database it lists `added`, `removed` and
`changed` (type) properties, and `renamed` properties (same ID, new name),
which are information and not drift. Databases without an ID are listed under
`skipped`.

**Renames and the scheduled check.** `t6 schema-check --fix-snapshot` rewrites
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

`t6 cameras get` and `t6 cameras update` let a skill read and change Cameras
without Notion property names. Both need the Notion extra, `T6_NOTION_TOKEN`,
and the same `--data-sources` file, which must have a `cameras` entry. Fields
use the generated model's names: `name`, `tags`, `related_to_projects_cameras`
and `wiki`, plus the row `id`.

```console
$ t6 cameras get --data-sources data-sources.json            # JSON list
$ t6 cameras get --page-id <page id> --data-sources data-sources.json
$ echo '{"tags": ["Sony", "Full frame"]}' | t6 cameras update <page id> --data-sources data-sources.json
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

`t6.llm` runs typed LLM tasks. Install it with `pip install 't6[llm]'`
(pydantic v2 is its only dependency). A `Task` has a prompt tag, an input
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
{"version": "0.3.3"}
```

`python -m t6 version` provides the same output. `t6 schema-check` is
described under [Schema check](#schema-check). `t6 migrate-projects` and
`t6 classify-projects` are described under
[Migrating existing Projects](#migrating-existing-projects); they read the
registry file at `--registry-db` (default `$T6_REGISTRY_DB`). `t6
drives-import-dry-run` is described under
[Importing the legacy workbooks](#importing-the-legacy-workbooks). `t6 health`,
`t6 backup --into DIR` and `t6 restore --from DIR` use the server's database
variables and are described in `docs/deployment.md`.

### Check an app follows the consumer contract

`t6 check-consumer PATH` scans the Python files under `PATH` and fails when
app code names a Notion, OpenRouter, DeepSeek or Frame.io API host in a string
(a direct HTTP call), or imports `t6.notion.generated`
([ADR 0002](docs/adr/0002-consumer-contract.md)). Docstrings and virtualenv or
cache directories are not scanned. It prints JSON and exits 1 when there are
findings (2 when the path does not exist):

```console
$ t6 check-consumer .
{"findings": [{"file": "app/notes.py", "line": 12, "rule": "direct-http", "detail": "names the Notion API host api.notion.com"}]}
```

To add it to an app's CI, install t6 at the pinned release tag and run it on the
app's source:

```yaml
- run: pip install 't6 @ git+https://github.com/mclbautista/t6-core.git@v0.3.3'
- run: t6 check-consumer .
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
git tag v0.3.3 && git push origin v0.3.3
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

- `ProjectServer(db, database, schema_for(version))` lists every project with
  database, folder path, name and internal ID. It refuses to start when the
  database role can write (`ReadOnlyViolation`). Connect with
  `t6.resolve.postgres.PostgresDatabase(dsn)`, which also sets the session
  read-only.
- A project's External ID is `<database>/<folder path>/<project name>`; a
  project in the root folder has no folder segment. `ResolveProject.ref` is the
  ExternalRef (system `resolve`, kind `project`, Internal ID the server's
  project ID).
- `map_projects(projects, external_links)` matches projects to External Links
  rows. `unmapped` holds projects with no row or no Notion Project;
  `orphan_links` holds rows whose project is no longer on the server.
- `schema_for(version)` gives the table layout per `DaVinci Resolve Version`.
  Tests run against a SQLite fixture per version in `tests/fixtures/resolve/`.

The table layout in `t6.resolve.schema` and the SQL in `t6.resolve.postgres`
have not been run against a live server. Check them first, then correct the
schema entries and fixtures. The versions in `SCHEMAS` (18, 19) are placeholders until
`DaVinci Resolve Version` is filled on active shows.
