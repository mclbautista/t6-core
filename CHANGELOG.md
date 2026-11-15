# Changelog

All notable changes to this project are documented here, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow [Semantic Versioning](https://semver.org/).

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
