# Changelog

All notable changes to this project are documented here, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `t6.client.APIError` carries the HTTP status as `status` (#370), so a client can tell 5xx from 4xx without reading the message. It is None for an error raised before a request was made.

- `drive edit` changes every Drive fact (#370): description, brand, model, capacity, format, owner, ownership, purchase date, warranty and remarks, with the rules of `drive add`; a blank value clears. `drive get`, `drive list` and `drive find` return a `version` (a digest of the Drive's facts), and `drive edit` takes an optional `expected_version`, refused with rule `drive_changed` at preview and at confirm when it is not current. The `DriveStore` gains `set_facts`.

- `drive assign` takes `remarks`, stored on the Assignment and shown by `drive get` (#370).
- `drive unassign` ends an Assignment by its number alone (#370): `project` is optional when `assignment` is given, so an Assignment whose Project is text only (a workbook import) can be ended. Without `assignment`, `project` is still required.

### Changed

- Drive custody verbs refuse a step that cannot follow the last one (#378): `drive receive` is refused for a Drive in custody or retired, `drive release` for one released or retired (the whole release, naming every such Drive), `drive return` unless the Drive is released, and `drive retire` for one already retired. The refusal has rule `custody`, comes at preview and again at confirm in the write transaction, and names the Drive and its status. A retired Drive can no longer be received or returned through the verbs.

### Fixed

- `drive exceptions` on a server without the `drives` extra (#371): the read reached the workbook importer, which imported `openpyxl` when it loaded, so the request crashed on the image. The importer now imports `openpyxl` only when it opens a workbook.

## [0.6.1] - 2026-11-16

Drive reads and inventory verbs over the API, for the Drive web app, and one fix found in the live checks of 0.6.0. No migration: the schema stays at 41.

### Added

- Drive reads and inventory verbs (#365): `drive list` (text, exact serial, needs review, Project with an open Assignment and status filters), `drive get`, `drive find`, `drive shipments`, `drive shipment` and `drive exceptions`, all read only and MCP tools, and `drive add`, `drive edit` and `drive promote-serial` with the `drives` permission. The `DriveStore` gains `list_drives`, `list_shipments`, `drive_deliveries`, `set_label` and `set_form_factor`.

### Fixed

- Frame.io file location and versions of a file in a plain folder (#366): Frame.io answers 422, not 404, when a folder ID is asked for as a version stack. The reads now take both to mean the file is in no stack, so the read API answers 200 with no stack instead of 502.

## [0.6.0] - 2026-11-16

Phase 3 of the t6-core shape (#178) and transcription (#180): Bays, Media locations, Drives, Timelines, Project Start, Google Calendar, MCP and Transcripts. Released by #279.

### Added

- Bays and Bookings in t6: `bay book`, `booking move`, `extend`, `cancel`, `get` and `list`, with overlap refused unless forced, and `admin bay migrate` for the Notion Bays rows (#255).
- Google Calendar: every Booking change writes its event to the Bay calendar (#260). A change made in Google Calendar runs through the booking rules, and a clash is reverted with a note (#270). Who can edit a Bay calendar follows the `bookings` permission, with a daily reconcile (#271). `t6 admin calendar resync` queues a sync for every Booking that is not cancelled and has not ended (#361).
- Media locations: `media link`, `unlink` and `list`, and `admin media migrate` for the Notion External Links database (#256). Frame.io reads, Resolve `map_projects`, Instagantt linking and the review message read them instead of External Links (#261).
- Frame.io project links: `frameio project link` and `unlink`, open to any staff Person, with one Frame.io project for one Project (#262).
- The Drive store on the t6 database, with custody events that are append-only, an exception queue and source rows (#257). Drive verbs: `drive receive`, `release`, `return`, `retire`, `assign` and `unassign` (#264).
- Swing Drives: `swing assign`, `unassign`, `return` and `overdue`, an optional return date, and `t6 admin drive import --swing` for the Notion Swing Drives database (#273).
- Transcripts: the Transcript entity and `transcript create` (#210), `transcript speakers` (#211), `transcript translate` with eval cases (#212) and `transcript get`. The transcribe worker reads only inside the NAS shares in `T6_TRANSCRIBE_SHARES` (#356). Translate is off by default (see below).
- Versions are recorded from Frame.io `file.versioned` and upload events (#265). The main worker finds the file's folder through the new Frame.io read route `GET /frameio/files/<id>/location` and `FrameioReader.file_location`, and needs `T6_FRAMEIO_READ_URL` and `T6_FRAMEIO_READ_SECRET`; without them that handler is off and the start-up log says so (#360).
- Timelines: `timeline link` and `timeline list`, with the stamp written by hand in Resolve (#266).
- `project start` (#267): one verb with a previewed plan, a preview by job. The main worker builds the plan (it needs `T6_PREVIEW_SIGNING_KEY` and `T6_NOTION_TOKEN`); confirm creates the Approval (new gate `project start`), the Deliverables with the milestone template of the Project Type, the Media locations and the initial Bookings once each. The plan lists the manual steps (NAS folders, the Resolve project, the Resolve timelines with their stamps) and Activity records them. Confirm also creates the Frame.io project and a folder per Deliverable through the `frameio` queue (#275).
- Unlinked Frame.io activity and the Link Frame.io project button (#272). Review activity on a Frame.io project no show holds is held in the t6 database (migration 41) and one operator card per Frame.io project asks a coordinator for the show; tapping it runs `frameio project link` and the held activity is released once, also when the project is linked another way. The `/project` card gets Link Frame.io project for any active staff Person; the API needs `T6_FRAMEIO_READ_URL` and `T6_FRAMEIO_READ_SECRET` for it. New allowed-tapper rule `role:staff`.
- Derived reads `deliverable bangko`, `hand-to-mouth` and `sabit` (#268).
- A read-only MCP server on the t6 API, `python -m t6.mcp`, with one tool for each read-only verb: 37 in all (#258).
- `t6 health` reports `waiting_jobs`: due jobs that nothing has taken for more than 15 minutes, and dead internal jobs (#359, #361).
- Optional extras `google` (adds `cryptography`) and `lookup` are installed in the container image (#359).

### Changed

- Frame.io reads, Resolve `map_projects`, Instagantt linking and the review message read Media locations (by kind and role) instead of the Notion External Links database (#261). The briefing feed writes nothing to Notion: no share-link rows, no Last Activity stamps; a live share is `active`.
- `external-link find` is answered from Media locations; its `page_id` is the Media location ID.
- The Frame.io worker's briefing feed reads Media locations from the t6 API on Sixto: it needs `T6_API_URL` and `T6_REGISTRY_READ_SECRET` (the API's read-only registry grant), both or neither; it no longer uses `T6_SERVER_URL` or `T6_WORKER_SECRET` for this. The `frameio` queue grant gets no registry read. `T6_NOTION_EXTERNAL_LINKS_DATA_SOURCE` is no longer read by any worker.
- The main worker reads Media locations for the review message from the t6 database when `T6_DATABASE` is set (the queue server it polls is the VPS edge and has no registry); HTTP only without `T6_DATABASE`.
- New Media location kind `instagantt-project` (ref = the Instagantt project ID as exported, one Project at a time). Instagantt linking reads it, `external-link find` maps Instagantt rows to it and the External Links migration imports active Instagantt rows as it. Kind `timeline` stays for `t6t_` Timelines only.
- The main worker reaches the registry through the t6 API at `T6_API_URL`; `T6_SERVER_URL` carries queue calls only. The jobs the API starts itself go to the internal job table in the t6 database (#358).
- Workers claim only the job kinds they have a handler for: `/queue/claim` takes `kinds`. A job a worker cannot run stays queued until one that can starts (#361).
- A job whose lease ran out with all its tries used is marked dead at once, also when no worker polls (#361).
- The Deliverables T6 ID property ID is in the schema snapshot and the generated model, so `deliverable.sync` no longer refuses for a missing ID (#227, #353).
- When the main worker has `T6_DATABASE` set (the production setup), review messages for a Frame.io project with no link are held until it is linked, instead of going to the default review chat (#272). Without it the message still goes to the default review chat.
- Drive verbs use the Drive store; a duplicate Assignment is refused (#359).

### Removed

- `t6.notion.external_links`. Only the migration's one-off row read remains, in `t6.media_migration`.
- `UnwiredReader` in the Frame.io reads (#360).

### Fixed

- Delivery share views are recorded by the main worker, because the Frame.io worker cannot open the t6 database (#354, #355).
- Transcript runs resume, a failed run unblocks the Transcript, confirms are idempotent and refusals say why; translate keeps unlabelled colon lines and records failures; dead runs are swept on checks (#357, #359).
- The MCP server stays read-only, reports the package version and masks by caller (#357).
- A Media location deactivated by hand goes to review in the migration; the Frame.io project retry is safe (#357, #359).
- The booking sync handler, the real Google Calendar adapter and the event content (#359). Calendar deletes after a Bay change, a token change at start, sharing lookups and non-user writers (#361).
- Project Start retries and the release of held Frame.io activity; a begun Start keeps its own Approval (#361).
- One kasado rule for `package get` and the floor reads, and bangko reads the delivery Package only (#361).
- Swing import follows the live Swing Drives schema (#273).

### Off by default

- `transcript translate`. The API returns Refused (`translate_disabled`) until `T6_TRANSCRIPT_TRANSLATE=true` is set on it. It stays off until the live run (#213) is recorded.

### Upgrade notes

1. Back up the t6 database before upgrading. Migrations 33 to 41 run on open; none is forward-only.
2. Deploy the queue server (VPS) before the workers. Workers now claim by job kind: `/queue/claim` takes `kinds`.
3. The main worker needs `T6_API_URL` when `T6_DATABASE` is set. `T6_SERVER_URL` stays for queue calls only. Without `T6_API_URL` the worker refuses to start.
4. The API needs `T6_FRAMEIO_QUEUE_URL` and `T6_FRAMEIO_QUEUE_SECRET` for Project Start, which creates the Frame.io project through the `frameio` queue. The Frame.io worker needs `T6_FRAMEIO_ACCOUNT_ID` only when the sign-in sees more than one Frame.io account, and `T6_FRAMEIO_WORKSPACE_ID` only when the account has more than one workspace. The Frame.io sign-in must be allowed to create projects and folders.
5. When the main worker has `T6_DATABASE` set (the production setup), review messages for a Frame.io project with no link are held until it is linked. Without it the message still goes to the default review chat. Link the active Frame.io projects right after the upgrade (`frameio project link`, the operator card, or the Link button on `/project`).
6. The MCP server is new (`python -m t6.mcp`). It lists the 37 read-only verbs. Its output is masked for the `mcp` service and for any caller that is not an operator.
7. `t6 health` reports due jobs waiting over 15 minutes.
8. The image now has the `google` and `lookup` extras. To give the Bookings that already exist their calendar events when the calendar goes live, run `t6 admin calendar resync`. `t6 admin drive import --swing` imports the Notion Swing Drives database once.
9. The Deliverables T6 ID property is in the snapshot (#353), so step 4 of the 0.5.0 go-live list is done.

### t6 database migrations

Migrations run on open.

| Version | Change | Forward-only |
| --- | --- | --- |
| 33 | Bays and Bookings | No |
| 34 | Media locations (history append-only) | No |
| 35 | Drive store (custody events append-only, exception queue, source rows) | No |
| 36 | Transcripts (history append-only) | No |
| 37 | Transcript speaker maps | No |
| 38 | Drive previews and Delivery drive links | No |
| 39 | Timelines | No |
| 40 | Calendar channels, sync tokens and event versions | No |
| 41 | Held Frame.io activity | No |

## [0.5.0] - 2026-11-15

Phase 2 of the t6-core shape (#178): delivery, linked chats, Versions, Concerns and abang. Released by #254.

### Added

- Milestones and the derived stage of a Project, with milestone templates per Project Type and Status projection to Notion (#233, #238).
- Package handoff: `package prepare`, `check`, `handoff`, `accept` and `reject`, checked against the Package Spec (#235).
- `deliverable deliver` with a drive or share carrier (#240), and `deliverable miss`, which tells a missed date from a delayed one (#239).
- Linked chats: `project link-chat` and `unlink-chat`, and the `/link` and `/unlink` commands (#237). Linked chats are delivered to t6 through gateway subscriptions, a pull and member lists (#243), and a Chat link follows chat events: moves, topic names, members and dead topics (#248).
- Versions: `version add` and `version lock` for picture lock (#241), and client cut approval on a Version (#246).
- Review messages go to the show's linked topic (#244) and name the file's Version (#241).
- Frame.io `share.viewed` is watched for Delivery shares only (#245).
- Concerns: a reply to the brief is kept as a Concern note (#236); Concerns close by data, with flags (#242); Concerns open and close from new PM Notes lines with `concern scan`, `concern changes` and `project notes` (#247); `concern list --raise-at-psr` reads quiet Concerns (#336).
- `Sixto (auto)` is a named service actor limited to the Concern verbs (#334).
- abang: `abang request` and `cancel` and `/abang` by reply (#249); abang candidates, the Yes/No tap, `/abang` by destination button and the 3-day nudge (#251).
- `person create` makes a Contacts page and registers the Person (#332).
- `t6 health --local` and `--ignore-history` (#337).
- `T6_FRAMEIO_ALERT_TOPIC` sends the Frame.io sign-in alert to a forum topic (#318).
- Write-back safety checks in `Fleet.sync` and `Fleet.machines` (#177).
- ADR 0007: t6 stores no chat text, with the brief-reply exception (#335).

### Changed

- `concern raise` takes only a Project coordinator as owner, and the waiting party must be named in the line (#230).
- A Chat link change updates the gateway subscription set at once (#243).
- The Package Spec reader finds properties by ID, not by name (#228).

### Removed

- The 0.4.0 command aliases. An old name now fails and names its replacement; see the rename table under 0.4.0 (#231).

### Fixed

- The worker retries an unreachable queue instead of exiting (#325).
- A PM Notes line is retried at most 3 times, and a line that cannot change its Concern is not sent to the LLM again (#247).
- A Concern opened from PM Notes is as old as its line (#247).
- The Refused messages for missing Notion property IDs name `t6 admin snapshot` (#351).

### Off by default

- Opening and closing Concerns from PM Notes on a timer. Set `T6_CONCERN_AUTOSCAN=1` on the API to turn it on. It stays off until the live run with human review is recorded (#250). `concern scan` by hand works without it.

### Before go-live

1. Back up the t6 database before upgrading. Migrations 22 and 27 are forward-only: to go back to 0.4.1, restore that backup.
2. Callers of the old command names must use the new ones first (rename table under 0.4.0).
3. Add the Package Specs data source to the file named by `T6_READ_DATA_SOURCES`, under the key `package_specs` (#234).
4. Deliverable projection still needs a **T6 ID** text property in the Notion Deliverables database and a new schema snapshot. Until then Deliverable sync jobs are refused; nothing else depends on it.
5. For linked chats, review messages in topics and abang, the main worker needs `T6_DATABASE`, `T6_GATEWAY_URL` and `T6_GATEWAY_SECRET`. For `version add`, the API needs `T6_FRAMEIO_READ_URL` and `T6_FRAMEIO_READ_SECRET`.

### t6 database migrations

Migrations run on open.

| Version | Change | Forward-only |
| --- | --- | --- |
| 17 | Milestones | No |
| 18 | Packages | No |
| 19 | Concern note message IDs and brief messages | No |
| 20 | Chat links, their history and previews | No |
| 21 | Deliverable date moves and misses | No |
| 22 | Packages rebuilt, delivery carriers | Yes |
| 23 | Linked chat cursor, message facts (no text) and members | No |
| 24 | Versions and Version locks | No |
| 25 | Concern milestones, date rules and dates | No |
| 26 | Delivery share views | No |
| 27 | Deliverable templates | Yes |
| 28 | Linked chat topics, notices and sends | No |
| 29 | abang requests, history and previews | No |
| 30 | abang candidates, drafts and draft options | No |
| 31 | PM Notes lines, pending changes and changes | No |
| 32 | PM Notes attempts | No |

## [0.4.1] - 2026-11-15

### Added

- Deliverable entity and its Notion Deliverables rows (#227).
- Package Spec reader and spec versions (#228).
- Approval request, grant and deny with named gates (#229).
- Concern record and verbs (#230).
- Project archive and person assign / unassign (#232).

### Changed

- The bundled Notion schema snapshot carries the live property IDs of the Job Order **T6 ID** and **T6 Notes** properties, so Job Order projection (`jo.sync`) no longer refuses for missing IDs (#313).

### Fixed

- In edge mode, an unsigned or badly signed request to the Frame.io comment route or the Notion page route answers 401 instead of dropping the connection (#311).
- Resolve refuses unverified project server schemas (#306).

### Before go-live

- Deliverable projection needs a **T6 ID** text property in the Notion Deliverables database and a new schema snapshot; until then the bundled snapshot has no ID for it.

### t6 database migrations

Migrations run on open. None is marked forward-only; take a backup before upgrading regardless.

| Version | Change | Forward-only |
| --- | --- | --- |
| 13 | Deliverables, Deliverable sync intents and migration receipts | No |
| 14 | Approvals | No |
| 15 | Concerns and append-only Concern notes | No |
| 16 | Project archives and project person roles | No |

## [0.4.0] - 2026-11-14

### Added

- Phase 1 architecture decisions cover credential ownership, entity identity and history, studio verbs, and separate outside-event and internal-work queues (#181).
- The t6 database gains numbered migrations, permanent prefixed IDs and bindings for all entity kinds (#182). Project Activity and append-only audit records are written together (#187); internal jobs support leases, retries and idempotency (#188).
- A typed verb registry exposes schemas, outcomes and `t6 commands --json` (#183). Edge mode accepts configured, verified webhook routes (#184). CLI commands use noun/verb names and administration moves under `t6 admin` (#189).
- Signed, single-use preview and confirm, idempotency, preview by job, and Project hold/resume (#190).
- Persons, Contacts migration and rebinding (#191), shifts for deadline calculation (#192), permissions and grants (#193), and allowed-tapper checks (#195).
- Job Orders owned by t6, raise/assign/reassign/note, state verbs and Notion projection (#194, #196). Notion edits restore identity projections, apply permitted state changes, or flag violations (#198).
- The authenticated t6 API serves verbs (#197), taps (#199), masked and ZDR-aware LLM work (#201), and typed reads for app cutovers (#202). `t6.client` and the CLI call the API (#200); consumer checks enforce the revised contract with expiring exceptions (#203).
- The 0.3.5–0.3.11 fixes shipped on `release/0.3.x` and are included in this release.

### Before go-live

1. Back up the t6 database before upgrading. Set a persistent `T6_PREVIEW_SIGNING_KEY` on the API; CLI users of registered verbs need `T6_API_URL` and `T6_API_SECRET`.
2. Add rich-text **T6 ID** and **T6 Notes** properties to the Notion Job Orders data source, then run `t6 admin snapshot` to record their property IDs.
3. Bind Notion users to Persons so Notion state edits can pass permission checks; see `docs/jo-state-transitions.md`.

### CLI rename table

Old names are removed in 0.5.0; until then they print a deprecation notice on stderr.

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

### t6 database migrations

Migrations run on open. None is marked forward-only; take a backup before upgrading regardless.

| Version | Change | Forward-only |
| --- | --- | --- |
| 1 | Baseline registry schema | No |
| 2 | Reserved no-op | No |
| 3 | Activity feed and audit backfill | No |
| 4 | Internal jobs | No |
| 5 | Project holds, preview tokens, verb requests and status sync intents | No |
| 6 | Persons and unique active Telegram bindings | No |
| 7 | Person migration receipts and nullable kind for migrated Persons | No |
| 8 | Person shifts | No |
| 9 | Person permissions and permission seed | No |
| 10 | Job Orders, notes, migration receipts and sync intents | No |
| 11 | Job Order state and state previews | No |
| 12 | Notion last-seen property values | No |

## [0.3.11] - 2026-11-14

- Wait up to 60 seconds for Frame.io share links before sending a review message (#301).

## [0.3.10] - 2026-11-14

- Account for clip length in review timing and include the file's Frame.io share link (#297).

## [0.3.9] - 2026-11-14

- Treat Frame.io comments without an owner as guest comments (#294).

## [0.3.8] - 2026-11-14

- Send a custom User-Agent on Frame.io and Adobe requests (#291).

## [0.3.7] - 2026-11-14

- Preserve a safe summary of Frame.io error bodies in `FrameioError` (#289).

## [0.3.6] - 2026-11-14

- Accept Notion's signed verification-token handshake while the webhook secret is unset (#284).

## [0.3.5] - 2026-11-14

- Print the Notion verification token to stderr from `python -m t6.server` (#281).
