# t6-core

The shared T6 Python library. This repository was renamed from **t6-notion**
before its first commit. The distribution and import namespace are `t6`.

Requires Python 3.11 or newer. Install the base package with `pip install t6`.
The base package has no third-party runtime dependencies and includes
`t6.domain`, `t6.lookup`, `t6.drives`, `t6.registry`, and the `t6` command.

Domain terms are defined in [`CONTEXT.md`](CONTEXT.md).

## Domain

`t6.domain` models T6 once, as immutable dataclasses that do no I/O:
Project, Episode, Deliverable, JobOrder, Contact, Studio, Machine (Bay),
Camera and ExternalRef.

- Each entity stored in Notion is identified by its Notion page ID. Two
  instances with the same page ID compare equal. Relations are page IDs.
- A Project also carries `t6_id`, its T6 Project ID from the
  [Project registry](#project-registry), or `None` when it is not known.
  Equality still uses the page ID for now.
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
  (Frame.io and Notion). The tailnet one serves `POST /tap` (the gateway),
  `/queue/*` (workers and callers) and `/registry/*` (the
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
- **Events.** A Frame.io `comment.created` starts or resets a 15-minute timer
  for its Frame.io project; when it fires it becomes a `frameio.review_done`
  job with the comment count. A Notion `page.properties_updated`,
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
  variables below are set. No other handler exists yet, so a worker without
  them refuses to start.

| Worker variable | Use |
| --- | --- |
| `T6_SERVER_URL` | required: the server's tailnet address, for example `http://100.64.0.1:8081` |
| `T6_WORKER_SECRET` | required: the bearer secret for the queue (the server's `T6_WORKER_SECRET` or `T6_FRAMEIO_WORKER_SECRET`) |
| `T6_WORKER_QUEUE` | required: the queue to poll (`main` or `frameio`) |
| `T6_WORKER_POLL_SECONDS` | optional: sleep after an empty poll, default `5` |
| `T6_NOTION_TOKEN` | main worker: Notion integration token, for the Project registry sync |
| `T6_NOTION_PROJECTS_DATA_SOURCE` | main worker: ID of the Projects data source |

| Variable | Use |
| --- | --- |
| `T6_FRAMEIO_WEBHOOK_SECRET` | Frame.io signing secret |
| `T6_NOTION_WEBHOOK_TOKEN` | Notion `verification_token` |
| `T6_TAP_SECRET` | the gateway's `T6CORE_SECRET` |
| `T6_WORKER_SECRET` | main worker: claims `main`, may submit to `main` and `frameio`, reads Projects and reports Notion sync results |
| `T6_FRAMEIO_WORKER_SECRET` | Frame.io worker: claims `frameio` |
| `T6_REGISTRY_SECRET` | registry caller that may register and read Projects |
| `T6_REGISTRY_READ_SECRET` | registry caller that may only read Projects |
| `T6_QUEUE_DB`, `T6_PUBLIC_BIND`, `T6_TAILNET_BIND` | database path, listener addresses |
| `T6_REGISTRY_DB` | Project registry database path, default `t6-registry.sqlite` |

Tests use `t6.server.testing.FakeClock`; no test opens a connection to a
provider.

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

## Drives

`t6.drives` models physical external drives (never Google Drive). It does no
I/O and needs no extra.

- A `Drive` holds serial, brand, model, capacity in TB, format, form factor
  (`FormFactor.PORTABLE` or `FormFactor.POWERED_DESKTOP`), owner (a Studio page
  ID, or `None` for T6), purchase date, warranty and remarks.
- The serial is the identity. `normalize_serial` removes notes in brackets,
  trims and upper-cases, so `" abc123 (old) "` is `"ABC123"`. A Drive needs a
  serial.
- `parse_capacity_tb` reads sheet text: `"4TB, P"` is 4.0 and `"500GB"` is 0.5.
  Text it cannot read raises `ValueError` naming the text.
- `DriveStore` is the interface the custodian's web app implements:
  `add_drive` and `get_drive(serial)`. Adding a serial that is already recorded
  raises `DuplicateSerial`. `t6.drives.testing.InMemoryDriveStore` is the fake
  for tests.
- Custody is an append-only log of `CustodyEvent`s: kind (`CustodyKind.RECEIVED`,
  `RELEASED`, `RETURNED`, `RETIRED`), date, the `Party` involved (a Contact or
  Studio), the Project, who logged it and an optional reason. `RETIRED` needs a
  reason (dead, lost, sold). `DriveStore.append_event(serial, event)` and
  `list_events(serial)` are the only ways in and out: events cannot be edited or
  deleted, so a correction is a new event and both stay in the list.
- `DriveStore.status(serial)` is derived from the last appended event
  (`DriveStatus.IN_CUSTODY`, `RELEASED` or `RETIRED`) and never stored. A Drive
  with no events has no status (`None`). `derive_status(events)` does the same
  over a list. An unrecorded serial raises `UnknownDrive`.
- Contacts, Studios and Projects are a `Ref`: a Notion page ID, or, when a name
  could not be resolved, the name as text with `unresolved` True so it can be
  reviewed.
- An `Assignment` holds label, `project` (a `Ref`), `role` (`Role.MASTER`,
  `BACKUP`, `OFFLINE`, `SWING` or `ARCHIVE`), start, end and remarks. No end
  means the Assignment is open. `DriveStore.add_assignment(serial, assignment)`
  adds one and never edits or closes earlier ones. `open_assignments(project)`
  lists the Drives still out for a Project; `assignment_history(serial)` lists a
  Drive's Assignments across Projects, oldest start first. An unknown serial
  raises `UnknownDrive`. A label can repeat on different Drives and Projects.

## Lookup

`t6.lookup` turns a loose mention into the right entity, over an `Index` of
`t6.domain` entities the caller supplies (no I/O). `Lookup` has one method per
entity type: `job_order(telegram_key)`, `project(code, name=)`,
`deliverable(text, project_id=)`, `contact(mention, team=)`, `machine(text)`
and `studio(name)`. Each runs its pipeline cheapest first (exact ID,
normalized, fuzzy, optional LLM `judge`, human review) and returns a
`Resolution` (entity or none, method, score, ranked candidates,
`needs_review`). Below the policy thresholds it never picks. Deliverables are
resolved within one Project; Contacts within the Project team first, then all.
`Lookup.propose_alias` returns an `AliasWrite` for Notion `Aliases`; it never
writes. Honorifics and other normalizers are configuration (`Normalizers`).
The default fuzzy scorer is stdlib-only; set `Policy(scorer=...)` to use
another.

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
| `server` | Webhook server and queue (stdlib only) |
| `worker` | Job workers (stdlib only) |

The `notion` extra installs `httpx`, the `llm` extra installs `pydantic` and the `resolve` extra installs `psycopg`.
The other extras are declared and currently empty; their dependencies will be
added when those adapters are implemented. The `notion` extra does not install Frame.io, Adobe sign-in, or
LLM libraries.

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

### External Links

`t6.notion.external_links.ExternalLinks(client, data_source_id)` reads and
writes the External Links database:

- `refs_for_project(project_id)` returns the Project's rows as ExternalRefs,
  Internal ID included. `links_for_project` also returns the row's page ID,
  Active and Last Activity.
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
3. A read is a page retrieve, a data source retrieve, or a data source query.
   A write is a page create or a page update.
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

`src/t6/notion/schema_snapshot.json` lists every T6 database and the exact
name and type of each property (captured 2026-11-09). It holds no select
option values, because they contain show and client names. The `t6.notion.generated`
module has one model per database, such as `BaysRow` and `JobOrdersRow`. Run
`uv run python scripts/generate_models.py` after the snapshot changes. A test
fails when the module is out of date.

- A model field maps a Python name to the exact Notion property name, so a
  trailing space (`Blocker `) or an emoji (`🗂️ Coordinator`) reads and writes
  correctly.
- `Model.from_page(page)` ignores live properties that the snapshot does not
  list. A missing or wrong-typed property reads as `None` or an empty tuple.
- `Model.to_properties(**values)` builds the `properties` body for
  `create_page` or `update_page`. A value of `None` clears the property.
  Formula, rollup, timestamp and similar properties are read-only.
- Select and multi-select values are sent by name, and Notion adds an option
  that does not exist. The in-memory fake does the same.
- Bays carries the Munki fleet fields: `serial_number`, `munki_manifest`,
  `munki_client_version` and `os_version` (text), `ring` (select) and
  `last_check_in` (date).

```python
from t6.notion.generated import BaysRow

body = BaysRow.to_properties(name="Bay 12", os_version="macOS 26.5", ring="pilot")
page = notion.create_page({"parent": {"data_source_id": bays_id}, "properties": body})
bay = BaysRow.from_page(page)
```

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
`changed` (type) properties. For `projects` it also lists `blank` Project
Codes and `duplicates` (codes on more than one page). Databases without an ID
are listed under `skipped`. The exit status is 0 when everything matches, 1
when something drifted or a Project Code is blank or duplicated, and 2 when
the command cannot run (for example, no token).

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
are kept byte for byte. It is ported from the watchdog masker in t6-jo-tg-ntn
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
{"version": "0.3.0"}
```

`python -m t6 version` provides the same output. `t6 schema-check` is
described under [Schema check](#schema-check).

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
git tag v0.3.0 && git push origin v0.3.0
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
uv run --locked python -m unittest discover -s tests -v
uv build
```

The tests exercise the public CLI and build a wheel, then install the base
package and the Notion extra into separate clean environments without an
index. CI runs these checks on push and pull request for Python 3.11–3.14.

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
