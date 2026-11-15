# Changelog

All notable changes to this project are documented here, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `project start` (#267): one verb with a previewed plan, a preview by job. The main worker builds the plan (it needs `T6_PREVIEW_SIGNING_KEY` and `T6_NOTION_TOKEN`); confirm creates the Approval (new gate `project start`), the Deliverables with the milestone template of the Project Type, the Media locations and the initial Bookings once each. The plan lists the manual steps (NAS folders, the Resolve project, the Resolve timelines with their stamps) and Activity records them.
- Frame.io read API route `GET /frameio/files/<id>/location` and `FrameioReader.file_location`: the main worker records Versions from Frame.io `file.versioned` and `file.upload.completed` events live (#360). It needs `T6_FRAMEIO_READ_URL` and `T6_FRAMEIO_READ_SECRET`; without them that handler is off and the start-up log says so. `UnwiredReader` is gone.

- Unlinked Frame.io activity and the Link Frame.io project button (#272). Review activity on a Frame.io project no show holds is held in the t6 database (migration 37, `frameio_held_activity`) and one operator card per Frame.io project asks a coordinator for the show; tapping it runs `frameio project link` and the held activity is released once, also when the project is linked another way. The `/project` card gets Link Frame.io project for any active staff Person: the T6 account's projects not held by a show, most recently updated first, with search. The API needs `T6_FRAMEIO_READ_URL` and `T6_FRAMEIO_READ_SECRET` for it. New allowed-tapper rule `role:staff`.

### Changed

- Frame.io reads, Resolve `map_projects`, Instagantt linking and the review message read Media locations (by kind and role) instead of the Notion External Links database (#261). The briefing feed writes nothing to Notion: no share-link rows, no Last Activity stamps; a live share is `active`.
- `external-link find` is answered from Media locations; its `page_id` is the Media location ID.
- The Frame.io worker's briefing feed reads Media locations from the t6 API on Sixto: it needs `T6_API_URL` and `T6_REGISTRY_READ_SECRET` (the API's read-only registry grant), both or neither; it no longer uses `T6_SERVER_URL` or `T6_WORKER_SECRET` for this. The `frameio` queue grant gets no registry read. `T6_NOTION_EXTERNAL_LINKS_DATA_SOURCE` is no longer read by any worker.
- The main worker reads Media locations for the review message from the t6 database when `T6_DATABASE` is set (the queue server it polls is the VPS edge and has no registry); HTTP only without `T6_DATABASE`.
- New Media location kind `instagantt-project` (ref = the Instagantt project ID as exported, one Project at a time). Instagantt linking reads it, `external-link find` maps Instagantt rows to it and the External Links migration imports active Instagantt rows as it. Kind `timeline` stays for `t6t_` Timelines only.

### Removed

- `t6.notion.external_links`. Only the migration's one-off row read remains, in `t6.media_migration`.

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
