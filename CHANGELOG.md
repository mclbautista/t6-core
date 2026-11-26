# Changelog

All notable changes to this project are documented here, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Skill `t6:` key `final_turn: submit` (#793, spec #791): opt-in. On the last allowed turn the runner sends no tools and a fixed instruction to hand in the answer now, marking unknowns "(?)", so a run that would have ended in `max_turns_exceeded` returns its answer, validated against the output schema as usual. Any other value is refused when the skill loads. Skills without the key behave as before.

## [0.6.28] - 2026-11-25

The Concerns sheet release (spec #732, #734 to #737), with the CRM keep-warm DM (spec #705), the schedule snapshot (spec #694) and the Drive Inbox on top. The Concerns sheet is a Google Sheets working view for Closers: the API exports the open Concerns as a CSV after the 21:00 autoscan, and a signed Sheet Submit applies Concern rows through the queue. The Telegram Closer list is gone, so `T6_CLOSER_TELEGRAM_IDS` is no longer read. The database moves forward through migrations 61 to 66 (schema 66: one job row, then the CRM stage and edit history, the CRM snooze, the Concerns sheet results and the Drive short names; all additive, none forward-only), so deploy by the usual path with a backup first; the Apps Script for the sheet is in the repository and is installed by hand.

### Added

- Verb `schedule snapshot` (#722, spec #694, operator): reads one tab of a Client Sheet, read only, with a merge-aware Sheets read (values, merges and fills of that tab; the tab's grid size is the coverage target) and returns a `ScheduleSnapshot/v1`. Typed client `schedule_snapshot` and `t6 schedule snapshot`; the Google credential stays with the API. Documented in `docs/api-read-verbs.md`.
- Sheet Submit (spec #732, #735): a signed POST to `/webhook/sheets/concerns` is checked on the edge (new routes-file verifier `sheets_hmac`: HMAC-SHA256 of `{timestamp}:{body}` in `X-T6-Sheet-Signature`, timestamp in `X-T6-Sheet-Timestamp`, five-minute tolerance) and queued as job kind `sheets.concerns_submit` on `main`. The main worker applies each row in order: a changed revision is refused ("Changed since your last refresh"), a Note goes through `concern note`, Close through `concern resolve` (a pending close), Undo close through `concern reopen`, and the store's refusals pass through; one refused row does not stop the others, and a re-delivered job writes nothing twice. The actor is `sheet:<email>`; `concern list` shows the email as `close_by_name`. Each row's result is stored (migration 65, `concern_sheet_results`) and shown as `last result` in the next export until the next Submit for that Concern; the worker then pushes a fresh export (a failed push is logged, never fails the job). The main worker now needs `T6_CONCERNS_EXPORT_URL` and `T6_CONCERNS_EXPORT_PUSH_SECRET` (push), `T6_PREVIEW_SIGNING_KEY` and, for names in the export, `T6_NOTION_TOKEN`; the edge routes file needs the new route and its `T6_SHEET_SUBMIT_SECRET`.
- Concerns sheet export (spec #732, #734): after each completed 21:00 autoscan, once pending closes are applied, the API pushes a CSV with one row per Concern that is not closed (Concern id, revision, Project code and name, text, owner, waiting on, State `Open` or `Closing after PSR`, who asked to close and when, source, quoted line, as of, last evidence, opened, days open, allowed actions, last result) to the VPS server. Dates are Manila dates. Allowed actions come from the rules: `Keep, Close`, `Keep, Undo close` for a pending close, `Keep` for a date-rule Concern. `last result` is empty for now. The edge server stores the latest file and serves it on the new public path `GET /concerns/export.csv` only with the read token in `X-T6-Read-Token` (404 otherwise). New settings: `T6_CONCERNS_EXPORT_URL` and `T6_CONCERNS_EXPORT_PUSH_SECRET` (API; empty turns the push off), and `T6_CONCERNS_EXPORT_PUSH_SECRET`, `T6_CONCERNS_EXPORT_READ_TOKEN` and `T6_CONCERNS_EXPORT_FILE` (edge). A failed push is logged and does not fail the run (#734).
- `lead list` now also returns display names (#705): `lead_owners`, `delegate_producers`, `line_producers`, `prod_coor_supes` (lists of Contact names) and `studio` (Studio name or null), resolved from the Notion pages with one Contacts and one Studios read per call (the Studios read needs the `studios` data source). The ID fields are unchanged.
- CRM snooze (#716, spec #705): `crm snooze --until DATE` (CLI `t6 crm snooze`, `POST /v1/crm/snooze`, `Client.crm_snooze`) pauses the caller's own CRM DMs through a date, required and at most 30 days from today (Manila); `crm unsnooze` ends it early. Open to any active staff Person; both are audited (`crm_snooze`, `crm_unsnooze`) and refusals are plain sentences the gateway shows as is. While someone is snoozed their items in the daily CRM DM go to the Post Producers (new `T6_CRM_POST_PRODUCER_TELEGRAM_IDS`) who are not snoozed, else to the default owner, with a "Covering for" line; the default owner's own items go to the Post Producers and stay put when none is free. Items return the day after the end date. New table `crm_snooze` (migration 64, `forward_only=False`).
- Quote and billing reminders in the daily CRM DM (#715, spec #705): a `Log` row of Kind `quote` or `billing` with a Remind on date is listed under "Reminders" from that Manila date until Done is ticked, whatever the Project's stage (named by its Notion name, or its `t6p_` ID when that cannot be read). Quotes go to the Post Producers, billing to the Post Producers, Accountants and Finance Heads, set by the new comma lists of Telegram user IDs `T6_CRM_POST_PRODUCER_TELEGRAM_IDS`, `T6_CRM_ACCOUNTANT_TELEGRAM_IDS` and `T6_CRM_FINANCE_HEAD_TELEGRAM_IDS` (read by the main worker; a bad ID stops it at startup, an unset role gets nothing). A holder who is also a Person gets one DM, and a snoozed holder's line is not repeated for a Post Producer who already has it. No migration.
- Daily CRM DM (#714, spec #705): at 10:00 Manila the main worker sends each Lead Owner at most one gateway DM, greeting them by Contact name and listing only their cold Leads: stage, days since last contact, up to three open Concerns, and a missing Lead contact (Delegate Producer, Line Producer, Prod Coor/Supe) or Studio. Cold after 28 days (Leads), 7 (Negotiations), 3 (Proposal Made), 2 (Onboarding); a Log row or a stage change resets the clock, and a Lead with neither counts from its registration. A blank Lead Owner, or one who cannot be messaged, goes to the Person in the new `T6_CRM_DEFAULT_OWNER_PERSON_ID`. Nothing due sends nothing; Sixto never messages clients. A recipient who cannot be messaged does not stop the others: everyone reachable is sent to, then the job fails naming who was skipped. Off unless `T6_CRM_SHEET_ID`, the service account key (`GOOGLE_APPLICATION_CREDENTIALS`, else the worker warns at startup and the CRM features stay off) and `T6_CRM_DEFAULT_OWNER_PERSON_ID` are set; the worker refuses to start when that Person is not active with a Telegram ID (it also needs the gateway, Notion token and projects data source). The Lead list is read from Notion in the worker. No migration.
- The main worker keeps the CRM Sheet's hidden `Projects` tab current (#712): one `Name · t6p_...` line per registered open Lead (incoming Status) and active Project (In-Progress, Airing), rewritten by a `crm.sheet_sync` job after a Project is registered or its Notion title or Status is edited. It is the first Sheets write, made as the service account with the `spreadsheets` scope, and is off unless `T6_CRM_SHEET_ID` is set. The CRM Sheet must be shared with the service account as Editor (`docs/deployment.md`).
- `t6.schedule` revision identity (#721, spec #694): `source_revision` is the request's Google revision, or else a `sha256:` fingerprint of the normalized values plus merge geometry. A new optional request field `source_ids` carries a client-supplied durable ID per cell onto the candidate as `source_id`; a cell address stays provenance only and, without an ID, no link is made between revisions. Moved-cell fixtures under `tests/fixtures/schedule/`.
- `t6.schedule`: the `ScheduleSnapshot/v1` contract and a pure extractor for the monthly-calendar layout (spec #694). Request and response models validate with `parse_request`, `parse_response` and `round_trip`; versioned fixtures and JSON Schemas are under `tests/fixtures/schedule/`. Single-day cells become candidates with ISO dates and provenance; a tab in another layout returns a typed `unsupported_layout` result. Layout in `docs/schedule-monthly-calendar.md` (#717).
- `t6.schedule` read coverage (#719, spec #694): `coverage` now states `read_range` against `total_range` and its `reasons` (`rows_short`, `columns_short`, `extent_unknown`, `merges_missing`), so a short first chunk is an incomplete snapshot, not a complete one with fewer candidates. Whole-source uncertainty makes every non-held group `incomplete`; a problem in one group still only holds that group. Groups list `days` as `read`, `blank`, `unread` or `invalid`, and `coverage.standing()` tells a planner a candidate in a held or incomplete group is unknown, not missing. The request's `merges` is now optional (null means no merge list was read). New held and truncated fixtures.
- `t6.schedule` keeps task text faithful (#720): repeated titles and titles starting with a digit stay separate candidates, a `(TBC)`, `Tentative:` or `[Tentative]` marker is kept as the candidate's `tentative` evidence (the title is unchanged), and a cell that is only `BREAK` or a column O month note becomes a non-task item beside `NA:` availability. Cell text is never acted on.
- `t6.schedule` reads multi-day merges (#718): one candidate with inclusive `start` and `due` taken from the merge's horizontal day boundaries, not its height, checked against month and year rollover and weekday alignment. A span with ends off the day boundaries, an end day with no number, or day numbers that do not run consecutively holds its group as `ambiguous_span` with evidence. An entry under a blank day, or a span starting there, leaves every day it covers `unread`, and `standing()` says `incomplete` for a day in a group that is not complete.
- `t6.schedule` infers the year from the weekdays (#718): a new optional request field `reference_date` limits the inference to the year before, of and after it (the `schedule snapshot` verb passes today's date), the start year that aligns the most blocks must be unique (`ambiguous_year` if tied), and a block that does not align is held alone as `weekday_mismatch`. `year` still overrides. Without `year` or `reference_date`, nothing is guessed.
- When a `chat link` Approval is granted or denied (via card tap or CLI), the main worker posts one HTML-escaped line in the chat and topic being linked: `Linked to Name [CODE]`, `Unlinked from Name [CODE]`, or `Link to Name [CODE] was not approved by <denier>` / `Unlink from Name [CODE] was not approved by <denier>` (with `: <reason>` when a reason is given, falling back to the Project id when neither name nor code is present, and to the Person id when the denier's Notion name is unavailable) (#687).
- Chat link Approvals (`project link-chat` and `project unlink-chat`) still `requested` 24 hours after they were made now send one reminder in the operator chat (`T6_OPERATOR_IDS`), naming the Project, chat and topic, requester, and Approval ID. The Approval never expires and no second reminder is sent; requests granted or denied within 24 hours send none. Migration 61 (`forward_only=False`) queues reminders for any `requested` `chat link` Approvals already in the database (#688).
- `project links` (CLI `t6 project links CODE`, `POST /v1/project/links`, `Client.project_links`): operator-only read returning a Project's links card — name, code, Notion page URL, Frame.io project URL (from its active `frameio-project` Media location), and every chat link with its chat, topic, topic name (when known), and active/inactive state (`#686`).
- `drive capture` reads the caption habit (spec #693): a leading IN or OUT, then Items on the same line or following lines, an `R: <name>` line for the recipient of an OUT, @tags and Count lines ("48 FD"). The capture result gains `items` (`text`, `state`), `counts` (`count`, `kind`), `recipient`, `tags`, `grade` (`complete`, `thin`, `problem`) and `problem` (`missing_direction`, `missing_recipient` or none). A caption with no IN or OUT, or an OUT without `R:`, is a `problem`; one with no Item and no Count line is `thin`; the rest are `complete`. An edit re-reads the caption. An `R:` line now names the person an OUT is released to (#723).
- `drive inbox` (`POST /v1/drive/inbox`, `Client.drive_inbox`, needs `drives`): the Items of kept Captures waiting on the custodian, newest Capture first, each with `capture`, `item` (its place in the Capture, from 0), `text`, `caption`, `direction`, `recipient`, `photo_reference`, `guesses` (empty until guessing exists) and the post's `chat_id`, `topic_id` and `message_id`. Count lines never appear, and a Capture whose caption has a problem waits for its poster's edit. `drive approve` (`Client.drive_approve`, needs `drives`) settles one Item (`capture`, `item`) with a `choice`: `drive` (a Drive ID in `drive`, or a label or serial in `typed`) appends the Movement, `new` creates the Drive (`label`, default the Item text; optional `serial`) and its Movement, `not_a_drive` closes the Item with no Movement, `none` leaves it waiting. A repeat of the settling call returns the same result; any other choice for a settled Item is refused (`item_settled`). A Movement made from a Capture stores `coordinators`: the Project's T6 Post Coordinator Person IDs (a Contact with no Person stays its Notion page ID) read from the Notion mirror at the move and never refreshed (#724).
- `project crm-links` (CLI `t6 project crm-links CODE`, `POST /v1/project/crm-links`, `Client.project_crm_links`): operator-only read of the CRM Sheet `Log` tab returning a Project's links grouped by kind (`quote`, `billing`, `gmail_thread`; a mail.google.com link is a `gmail_thread` whatever its Kind) in one call. New `t6.crm` package reads the Log by header name, maps rows to Projects by the `t6p_` ID (rows with no or an unknown ID, or no date, are reported, not guessed), gives last contact as the later of the newest Log row and the last stage change (taken through an injected lookup), and lists due `quote` and `billing` reminders. The Sheet is set by `T6_CRM_SHEET_ID`; unset turns CRM reads off. No migration (#711).
- CRM stages (#709, spec #705): a known Person's Notion Status edit to Leads, Negotiations, Proposal Made, Onboarding or Lost is accepted (it was flagged) and recorded in the new append-only `project_stage_history` (migration 62, `forward_only=False`): who, when, from, to, shown in Activity as `crm_stage_changed`. An edit from a stage to In-Progress is accepted as the end of the CRM part, not a `resume`. An unknown editor is still flagged. The worker sends the Status option ID with the edit and `t6 admin snapshot` now records the stage option IDs under `projects` `named_options`, matched ignoring emoji and case, so a renamed option is still found. Onboarding joins the incoming status group. `Project` gains `lead_owner_ids`, `delegate_producer_ids` and `line_producer_ids`.
- The main worker writes each CRM edit of a Lead to the CRM Sheet `History` tab (#713): one row per Notion edit of Status, Lead Owner, Delegate Producer, Line Producer, Prod Coor/Supe or Studio, with Date (Manila), Project, What changed, From, To and By, as names, newest first. Edits are kept in the new append-only `crm_edit_history` (migration 63, `forward_only=False`, unique per Notion event and property, so a replayed event adds no row) and a `crm.history_sync` job rewrites the tab. A Lead's Status edit to a non-CRM status (Standby, Archiving, Airing...) is a row too, From its current stage. Other properties and Projects that are not Leads are ignored. A relation's From is blank when t6 had not seen its earlier value. The `Projects` dropdown now lists open Leads from the CRM stage history (any stage but Lost) and active Projects from Notion Status.
- `lead list` (CLI `t6 lead list`, `POST /v1/lead/list`, `Client.lead_list`, MCP `lead__list`): reader read of the open Leads with stage, Lead Owner, Studio, Delegate Producer, Line Producer, Prod Coor/Supe and days in stage (#709).

- A new Projects page is registered as a Lead when it is created (#710): the main worker handles the `page.created` event by registering the page (out of rotation, no code, a `t6p_` ID, keyed by the event ID so a replay registers once) with no operator card, and sets a blank Status to Leads. The relation names it shows then are the baseline for the History tab's From. A page created with another Status keeps it, and a CRM Status is recorded as its first stage. Pages seen only on an edit are still recorded as review items. New sync-grant route `POST /registry/pages/<page>/register`; no migration. ADR 0001 now says page creation is the explicit registration.

### Changed

- Timed catalog scans (#764, part of `t6-drives#55`): `drive catalog-snapshot` (`captured_on`) now carries an ISO 8601 date, time and UTC offset (normalized to Asia/Manila) instead of only a day, so several scans of one Drive on one day are all kept in chronological order and each diff is against the scan before it. A scan at or before the Drive's latest scan by instant is still refused with rule `catalog_order`, and a timestamp without a UTC offset is refused with `input`. A bare `YYYY-MM-DD` date keeps working for existing callers and is read as the start of that day (`00:00:00+08:00`); `drive catalog-at` with a bare date returns the latest scan up to the end of that day (`23:59:59.999999+08:00`) and also accepts an aware timestamp. Snapshot reads (`drive catalog-snapshots`, `drive catalog-diff`, `drive catalog-at`) return the full timestamp. Migration 67 (`forward_only=False`) rebuilds `drive_catalog_snapshots` without `UNIQUE (drive_id, captured_on)` while keeping all existing rows.
- The Telegram Closer list is removed (spec #732, #736): `T6_CLOSER_TELEGRAM_IDS` is no longer read. `concern resolve` and `concern reopen` over the API follow the verb's `coordinator` role only (the rule `closer` is gone). `project notes-history` is readable by Persons with `master_brief` on and the Project's coordinators; everyone else is refused with rule `coordinator`.
- `drive capture` no longer reads the photo or makes Movements. A kept post is `resolved` with its Items open in the Inbox; no Capture becomes an exception any more, and a caption with a problem stays `resolved` until its poster edits it. `place` is no longer read from the caption. Follow-up answers and a Drive given to an exception no longer apply: approve the Item instead (#724).
- The Approval for `project link-chat` and `project unlink-chat` (the Telegram card "Approval: chat link") now names the Project by its Notion name and code before the Project id, for example `Big Show [ABC123] t6p_...: chat -100, topic 12 (Edit)`. The ids stay in the target. The name is read from the Project's Notion page when the preview is made; if it cannot be read, the code (and, without one, only the id) is shown as before. The topic name was already shown when known.

## [0.6.27] - 2026-11-24

The Drive web pages release (spec #665). Going live is now the setting `T6_DRIVE_LIVE=true`, read on start (ADR 0010), and the `drive cutover` verb is removed. Adds movement photos, `drive recycle`, the Contacts picker reads and writes, and the latest movement per Project. It also includes the quiet TRANSMITTAL capture work (spec #659) and fixes for `drive mapping apply` and title rows in the tracker. Schema: migration 60 adds `drive_movement_photos`. The API and the main worker need `T6_DRIVE_PHOTO_DIR` (the `t6_photos` volume, netops #176). Do not set `T6_DRIVE_LIVE` before the cutover on 2026-12-03. Nothing is deployed by this release.

### Added

- `drive capture` reads IN and OUT in any case, followed by a space, colon or dash ("IN: SHELF", "in - table", "OUT: Marco", "Out-Marco"). A post that names a Drive but has no IN or OUT is an exception with reason `missing_direction`. A follow-up answer that starts with IN or OUT supplies or replaces the direction, person and place; other follow-up text is a label or serial. The capture result gains `reason` (`missing_direction`, `unreadable_label`, `no_drive_match`) and `may_ask`, true on the first exception and false once a follow-up answer has been tried (#660).
- `drive capture` with an existing `capture`, new `text` and `edit: true` replaces the caption of a pending or exception capture and reads it again as a new revision, so a fixed caption resolves it. An edit is not the follow-up answer: `may_ask` is unchanged and any follow-up already given is kept. Edits of resolved, undone or ignored captures change nothing and return the current result (#662).
- `drive capture undo` is refused with rule `undo_window` more than one hour after the capture was resolved. The capture result gains `undo_until`, the ISO datetime the window closes, for resolved captures (null otherwise, and null with no limit for captures resolved before the time was recorded) (#662).
- `drive capture` accepts a recording-channel post with no photo (the configured chat and topic are still enforced). If Jev keeps it, it becomes an exception with reason `no_photo` (`may_ask` false), listed by `drive capture list` with photo none, and is never sent to the image model (#661).
- Drive debug topic: with `T6_DRIVE_DEBUG_CHAT_ID` (and `T6_DRIVE_DEBUG_TOPIC_ID`) set, the main worker posts one plain-text message per Capture through the gateway's `/notify` and edits it through `/edit` as steps finish: poster Telegram ID and Person, message link, withheld caption, Jev verdict with match/maybe/no probabilities and model, photo readings and model, parsed direction/person/place, matched Drives, state and reason, follow-up answer, edit, undo and undo window end, per-step latency, errors, and a final marker. `T6_DRIVE_DEBUG=on|off` switches it; unset it is on while practice mode is on. No photo, no buttons; a failed debug call is logged and never changes the Capture. An undo through the API queues a small `drive.capture.debug` job, only once the message exists, so the worker refreshes it (#663).
- Movement photos: `movement photo-add` (JPEG, PNG or WebP, at most 10 MB, one per movement; adding again replaces it), `movement photo-get` and `movement photo-delete` (only the drive custodian or an operator). Files are kept in their own folder, `T6_DRIVE_PHOTO_DIR`, with a pointer in the database (migration 60, not forward-only) and never go to `t6.llm`. `movement list` items carry `photo` (`upload`, `capture` or none). A reload in practice mode removes the photos of the movements it replaces, after it commits (#669).
- `drive recycle`: gives a Drive a new label and Project in one step (preview, then confirm; needs the `drives` permission). It ends the Drive's open Project uses, starts one for the chosen Project with the new label (role master unless given), and keeps the old label in the Drive's remarks. A retired Drive, an unknown Project, a blank label and an unchanged label and Project are refused (#670).
- `contact find`: a short type-ahead list of Contacts (name, Telegram handle) over the Notion mirror, for the Drive web app person picker; needs the `drives` permission, and masks names for a caller that cannot read real names (#668).
- `contact create`: adds a Contact (name, optional Telegram handle and phone) marked "Added from Drives", preview then confirm; a blank name or an existing name is refused (#668).

### Changed

- Jev's transmittal check now runs before the photo read, on the withheld caption and a has-photo flag only (no photo readings). Only an explicit "no" discards the post, without calling the image model; a failure or "maybe" keeps it. Follow-up answers and custodian-picked Drives skip Jev. Prompt tag `drive-transmittal-v1` becomes `drive-transmittal-v2`: a live eval run of v2 is an owner step and must be recorded before the release that contains it. ADR 0009 is amended (#661).
- `drive projects` returns `latest_movement` for each Project: the newest movement on any of its Drives, or none, so the Drive web app can sort Projects by it (#667).
- Going live is the setting `T6_DRIVE_LIVE=true`, read once when the API and the main worker start (ADR 0010). The first start with it on records live mode in the database: Practice is off in every read and reply, the nightly reload is not scheduled, and every reload is refused with rule `cutover`. Nothing is reloaded at start. Live mode is one way: it stays on if the setting is later removed, with a warning in the log (#666).

### Removed

- The `drive cutover` verb (client method `drive_cutover`, CLI and docs). Use `T6_DRIVE_LIVE=true` (#666).

### Fixed

- The tailnet listener takes a request of up to 14.5 MB on `/v1/movement/photo-add`, so a 10 MB photo (about 14 MB in base64) reaches the verb instead of the 1 MB body limit (#669).
- The tracker reader no longer reports a title row above the header row (a row with one filled cell, such as the tracker name on the monthly tabs or the tab name on the archive and swing tabs) as an unreadable row. A stray one-cell row below the header is still reported. The first reload in practice mode listed 14 such title rows as exceptions (#658).
- `drive mapping apply` now works through the API and the `t6` command line: `--file` is a local file that the client uploads, and the preview token no longer depends on the temporary path of the upload, so a confirm matches its preview. `drive mapping propose` takes no output file over the API, reads the two configured Google spreadsheets when no workbook is given, and takes the data sources from `T6_READ_DATA_SOURCES` when none is uploaded. A sheet name that cleans to nothing is skipped by `propose`, and `apply` names it in its refusal (#657).

## [0.6.26] - 2026-11-24

Fixes the Google Sheets read for `drive reload`, the nightly reload and `drive mapping propose` (#654, PR #655). 0.6.25 read the whole sheet grid, which stalled the host on the per-project sheet (hundreds of tabs, millions of empty cells). The read now takes only the cells that hold data and has size and time limits. No schema change. Nothing is deployed by this release.

### Fixed

- The Google Sheets reader for `drive reload`, the nightly reload and the mapping proposal read the whole grid (`includeGridData`). On a sheet with hundreds of tabs and millions of empty cells that returned a response big enough to stall the host. It now reads only the cells that hold data, with `values.batchGet` (15 tabs per request, serial dates), and fails the read when a sheet has more than 3,000,000 cells, takes more than 180 seconds, or a response is larger than 64 MiB. The importer reads a serial number in a date column as a date (#654).

## [0.6.25] - 2026-11-24

Reads and edits for the Drive web app's Projects and Exceptions pages (#649): the Drives of a text-only Project, the list of unresolved captures with their photo, giving a capture a Drive, a Project on a movement correction, and the last reload report. No schema change. Nothing is deployed by this release.

### Added

- `drive list` takes the text of a text-only Project in `project`, and lists the Drives with an open Assignment to that text, as it does for a registered Project ID (#649).
- `drive capture list` (Drive callers only): captures by state (`exception` by default, or `pending`, `resolved`, `ignored`, `undone`), newest first, each with its text, sender and photo size and type. An ignored capture has no text and no photo. `drive capture photo` (Drive callers only, not an MCP tool) returns the kept photo; no photo goes to any other service (#649).
- `drive capture` with a `capture` takes a `drive` ID (Drive callers only): the custodian gives an unresolved capture a Drive, and it resolves as a normal Movement without a new photo read (#649).
- `movement correct` takes a `project`, a registered Project ID or the text of a text-only Project. The correction takes the movement off the needs-a-Project list and the original entry stays (#649).
- `drive reload report`: the last stored reload report, with its counts, `unmapped_projects`, the rows that could not load (`exception_rows`) and the reload time (`at`). `report` is null before any reload (#649).

## [0.6.24] - 2026-11-24

The Drive web app can search by Project code and show who has each Drive and where it is (#646), and the API gains the Concerns rollout and JO Health (spec #635). The schema moves from 55 to 59 (additive migrations, none forward-only: 0.6.23 still opens it). Breaking: `concern snooze` is gone (see Removed). Nothing is deployed by this release.

### Added

- `drive list` searches Assignment Project codes; `drive list` and `drive get` return the custody-log holder and Place, sharing the existing free-text `office_location` with IN, OUT, movement corrections and `drive edit` (#646).
- Job Orders: the On Hold Status, in the not-started group, so it never counts as processed. `jo list`, `jo get` and `jo find` return `health_name`, one of the six Health values (Closed, Blocked, Delayed, Urgent, Advanced, On Track) read by its words, ignoring emoji; `health_flags` stays until its callers move (#637).
- `project notes-history`: every dated PM Notes line each autoscan read (first seen, last seen, removed), newest first, for Closers (`T6_CLOSER_TELEGRAM_IDS`) and the Project's coordinators. Migration 57 adds `pm_notes_history` and `concern_autoscan_runs`. Not forward-only (#638).
- `concern list` returns a second list, `pending`, in both modes: every Concern with a pending close, with every field of an `--open` item plus `close_by`, `close_by_name`, `close_at`, `close_source` (`close` or `done_line`) and `close_quote`. A Concern with a pending close is only in `pending` (#639).
- The Closer setting decides `concern resolve`: a Person whose Telegram user id is not on `T6_CLOSER_TELEGRAM_IDS` is refused with rule `closer` (#639).
- `person list` items carry `master_brief` (boolean), so the reports app reads it in one call.
- Migration 59 adds `settles_id` to `concern_pending_closes`: a Close's settlement is set on the Concern when the close is applied, and dropped by an Undo.

### Removed

- Concern snooze: the `concern snooze` verb and the `snoozed` state are gone; `/v1/concern/snooze` answers 404. Migration 56 reopens every snoozed Concern and clears its `manual_override`; no row is deleted. Not forward-only (#636).

### Changed

- The autoscan serves a run time that passes during a run instead of skipping it, stores the slot it served, and on the first start after the upgrade runs no scan (it waits for the next 21:00).
- A pending close is applied only for a Project whose lines the run read in full; the others stay pending for the next completed run.
- Undo (`concern reopen` of a Concern with a pending close) is refused with rule `closer` for a Person who is not a Closer. `concern reopen` of a quiet or expired Concern with no pending close opens it with the override again.
- `concern list --raise-at-psr` lists every Concern that is not closed; it no longer filters on snoozed (#636).
- Concern autoscan: runs once a day at `T6_CONCERN_AUTOSCAN_AT` (default 21:00 Manila) instead of on an interval, with one catch-up at start when the last slot was missed; `T6_CONCERN_AUTOSCAN_SECONDS` is gone. Each run archives PM Notes before scanning (#638).
- Closes wait for the next PSR: `concern resolve` and a Done line (the scan's `settles`) record a pending close instead of closing, and no longer schedule a `concern_pm_close` timer (a queued one is acknowledged and does nothing). The Monday 21:00 autoscan applies, at the end of its run, every pending close made before that Monday 08:30 Manila; a failed run applies nothing and the next completed run applies what it left. A continuing line before then is a veto; `concern reopen` on a pending close is Undo. `manual_override` is set only when a Close is applied. Data closes stay immediate. The run log adds `applied=N`. Migration 58 adds `concern_pending_closes` and moves the open week-timer rows into it; `concern_pm_pending` stays, empty, so the previous release still opens the file. Not forward-only (#639).

## [0.6.23] - 2026-11-24

Drives are loaded from the custodian's sheets, and drive movements are recorded from the TRANSMITTAL chat. The schema moves from 50 to 55 (additive migrations, not forward-only: 0.6.22 still opens it). Nothing is deployed by this release; the gateway and the web app follow in their own repositories.

### Added

- The recording channel is chat ID plus topic ID.
- ADR 0009: TRANSMITTAL is a configured recording channel that amends ADR 0007 for one chat, plus the drive glossary terms (#628, part of #615).
- `drive reload`: replaces all Drive data from the per-project sheet in one transaction, keeps each accepted serial's Drive ID across reloads, and writes a reload report (#629).
- `t6.llm` image input and a drive photo-read task that returns labels and maker serials (#630).
- `drive mapping propose` and `drive mapping apply`: a checked mapping from sheet Project names to registered Projects, with unmapped names listed in the report (#631).
- Movement records, tracker loading, `movement list`, `movement correct` and Project counts (#633).
- A Google Sheets source (read-only), a nightly reload at midnight Manila time, practice mode and `drive cutover` (#634).
- `drive overdue` and a daily 08:00 Manila reminder to the configured custodian for T6 Drives out more than 14 days (#641).
- `drive capture`, its resolution job, follow-up labels, `drive capture undo` and the capture result read (#642).
- A Jev filter that ignores TRANSMITTAL posts that are not drive transmittals and keeps nothing from them (#643).

### Fixed

- Frame.io: a share scan stops after a time budget so it cannot hold the worker for minutes (#627).
- Small gaps left after 0.6.8 (#614).

## [0.6.22] - 2026-11-23

The Concern scan stops re-sending lines whose reply is invalid, and drive reads and privacy masking are faster. The schema moves from 49 to 50 (index-only migration, not forward-only: 0.6.21 still opens it).

### Fixed

- Concern scan: a PM Notes line whose LLM reply fails the output schema now counts as a rejected answer, and the line is held after 3 attempts instead of being re-sent on every daily scan (#610, part of #504).

### Changed

- Drive reads: indexes on the Drive child tables (migration 50) and a paginated drive list, so inventory, shipment and drive-get reads avoid full table scans (#609).
- Privacy masking: a faster matcher for registered names (#609).

## [0.6.21] - 2026-11-23

Guest reviewers' names on passphrase shares. No migration: the schema stays at 49.

### Fixed

- Guest names on passphrase shares: the Frame.io worker sends `x-frameio-share-authentication` as base64 of `<share id>:<passphrase>`, the way Frame.io's web app builds it, instead of the guessed unlock call, which does not exist. It also sends the web app's client headers (`apollographql-client-name`, `apollographql-client-version`, `x-gql-op`), without which Frame.io answers PERMISSION_DENIED (#602).

## [0.6.20] - 2026-11-22

The Frame.io review message tags T6's coordinator. No migration: the schema stays at 49.

### Fixed

- The Frame.io review message tags the linked Project's `T6 Post Coordinator`, not `Prod Coor/Supe`: that is the production's post supervisor, a client (#547).

## [0.6.19] - 2026-11-22

Guest reviewers' names in the Frame.io review message, and a longer CLI wait for `concern scan`. No migration: the schema stays at 49.

### Fixed

- The Frame.io worker fetches a share page as `t6-frameio-worker`: Frame.io answered Python's default User-Agent with a bare 403 page, so a guest's name was never read (#545).
- A guest's name now comes from the comments Frame.io's share GraphQL API returns (`GetShareCommentsByAsset`, `owner.name`), unlocked with the share's passphrase from the v4 share object. The unlock call is a best guess until it is captured (#602); any failure keeps "A guest" (#545).
- `t6 concern scan` from the CLI no longer gives up after 10 s with `unavailable: timed out` on a Project with many new PM Notes lines. The CLI now waits 600 s for `concern scan`, and `T6_API_TIMEOUT_SECONDS` sets the wait for every CLI call. A timed-out call names that setting. The API had kept scanning after the CLI left (#596).
- The concern scan log line counts the Projects in rotation it skipped (no coordinator, or notes that could not be read) as `skipped=N` (#596).

## [0.6.18] - 2026-11-22

A patch for the watchdog after the 0.6.17 deploy: `llm run` refused its output schema (rule `masking`) because a Contact's full name is an ordinary word that the schema also holds. No migration: the schema stays at 49.

### Fixed

- In a prompt template, output schema or question (`FIXED_TEXT`), a person's name or alias that is an ordinary word is no longer a name, as for Project names and codes since 0.6.9 (#498). Masking of the input is unchanged.

## [0.6.17] - 2026-11-22

Fewer Notion calls when a Job Order is raised and read back (#512), and planner bitrate fixes. No migration: the schema stays at 48.

### Changed

- `jo.sync` create makes 6 Notion calls instead of 10: the new `JobOrders.insert` creates and checks the row and body without the final full read, and the projection takes JO Status from that row and skips `ensure_body` after its own create (#591).
- `jo get` with a page builds the Job Order from the page it retrieved for the parent check: 2 Notion calls instead of 3 (#592).
- OCF Drive Planner: missing bitrates are filled and unsupported modes dropped (#24).

## [0.6.16] - 2026-11-22

Timeline stamps by code, the Resolve worker, and the bay scanner reader and drift report. Migration 48 takes the schema from 47 to 48 (Timeline stamp state, `replaces` and `resolve_uuid`); it is not forward-only.

### Added

- The Resolve worker: `python -m t6.worker` with `T6_WORKER_QUEUE=resolve` runs `resolve.stamp` jobs on one Mac with Resolve Studio, through the tailnet routes `/v1/resolve/{claim,extend,report,fail}` that only the `resolve` identity may call. It never opens the t6 database. See the README, "Resolve worker" (#581).
- Bay scanner: a read-only reader of a bay's local Resolve disk library (#377), the bay scan report and list verbs, the scan script and the drift report (#376, #379, #380).
- A public camera-specs CSV for the OCF Drive Planner (#24).
- `t6.resolve.timelines.ScriptingTimelines`: the real `TimelineReader` over the Resolve scripting API, and the stamp writer. It writes the `t6t_` marker on a timeline's first frame (or updates it to re-stamp a duplicate), reads it back and saves; it refuses a timeline with a different stamp or a stamp already on another timeline, and gives a reason when the project cannot be loaded. It closes each project it loads and restores the current database (#580). The Resolve worker exits 2 when `DaVinciResolveScript` cannot be imported, including when it cannot load `fusionscript`.
- Verb `timeline restamp <project> <timeline> --stamp <t6t_>` (staff, previewed): the duplicate of a Timeline gets a new `t6t_` ID, is recorded as a pending Timeline and a `resolve.stamp` job (with `replaces`) re-stamps it (#582).

### Changed

- ADR 0002 rule 10 allows one Resolve write: the Resolve worker writes the `t6t_` stamp marker on a timeline's first frame, so stamping is no longer manual (#578).
- `timeline link` stamps by code: confirm records the Timeline `pending` and queues one `resolve.stamp` job in the same transaction (a retried confirm queues nothing more); the job's result marks it `stamped` or `failed` with the reason, and linking a failed Timeline again queues it again (without `--stamp` it keeps the failed Timeline's stamp, found by label; a new name becomes its label, in Audit; a failed re-stamp still replaces the duplicated stamp). A job is tried once: a worker that stops mid-job leaves it dead when its lease runs out, and its Timeline failed. `timeline restamp` also refuses the canonical Timeline by the Resolve unique id its stamp was verified on (the worker checks it again), a Project that is not a T6 Project ID and an inactive Resolve project. `timeline list` shows `stamp_state` and `stamp_reason`. Project Start's manual step is now "Resolve timelines" (create them by hand; `timeline link` writes the stamp). Migration 48 adds the stamp state, `replaces` and `resolve_uuid` to `timelines`; rows linked before count as `stamped`. It is not forward-only (#582).

## [0.6.15] - 2026-11-22

The Notion mirror, one privacy registry, human-only Concern snooze, no more Notion-edit pings, Frame.io review details and Drive catalog snapshots. Migration 47 takes the schema from 46 to 47 (Drive `qr_code` and the catalog snapshot tables); it is not forward-only.

### Added

- A local SQLite WAL mirror shared by the Notion readers, with per-read freshness `mirror` or `live` (#511).
- One `privacy_registry.json` for masked reads and LLM calls (#538). A new or changed Project rebuilds it by itself: applying a Project start, and registering or changing a Project, queue a `privacy_registry.rebuild` job with no name in it; the main worker rebuilds the file when it has `T6_NOTION_TOKEN`, `T6_READ_PRIVACY_REGISTRY` and `T6_READ_DATA_SOURCES` (#539). The file is written with mode 0600 (#534).
- **Start Project** on the operator `/project` card (#540). An operator can queue a Project Start for a Project, check its plan and confirm it, which requests the `project start` Approval and offers its card. Operator only; see `docs/operator-actions.md`.
- `llm run` routes one provider and API key per task tag (#541); a multi-turn skill loop with a privacy airlock in `t6.llm.run` (#524), and configured skills on `LLMService` and `/v1/llm/run` (#525).
- Verb `concern snooze`, for human operators only: it refuses the automatic actor, services and non-operators (#543).
- Deadlines: "tomorrow" resolves to the end of shift, or 23:59 when no shift is known (#544).
- Frame.io review messages show the comment timecode and the clip's total timecode (#546), a guest reviewer's name read from the public share page when the comment has no owner (#545; best effort, falls back to "A guest"), and end with the `@handle` of the linked Project's `Prod Coor/Supe` read from the Notion mirror (#547).
- Drives carry `office_location` and a unique `qr_code`; `drive find` takes `qr_code` and `drive list` takes `office_location` (#549). Every Drive's `version` changes once on this release.
- Drive catalog snapshots: `drive catalog-snapshot` records a dated scan as the changes since the last one; reads `drive catalog-snapshots`, `catalog-diff` and `catalog-at` (#549). The request limit is 16 MB for `POST /v1/drive/catalog-snapshot` on the tailnet listener only; every other path keeps 1 MB.

### Changed

- Concerns no longer go quiet or expire on a timer; `concern_silence` jobs are no longer scheduled, and old ones change nothing (#543).
- `concern list --raise-at-psr` lists every Concern that is neither closed nor snoozed, oldest evidence first, with `evidence_at`; the `label` field is gone (#555). Rows an older release left `quiet` or `expired` are listed as open.
- Flagged Notion edits to Job Orders and Projects no longer message the operators, and unbound Notion pages are no longer offered to the operator chat; the review item, audit, activity and last-seen state are kept, and queued `notion.state_flagged` jobs drain without a message (#548).
- PM-notes LLM scanning is separate from the deterministic `ConcernStore` (#542); timeline link, project start and transcript confirm run on verbflow (#561); one Notion names builder and one Identities protocol (#560).

### Fixed

- A refused write verb no longer drops the sync intents queued with it (#562).

## [0.6.14] - 2026-11-21

Bays name matching: `Deco 203` is no longer confused with `Deco 202`. No migration: the schema stays at 46.

### Fixed

- `machine sync` and `machine edit` no longer treat `Deco 203` as `Deco 202`: a row with exactly the machine's name (case and spaces folded) now wins, and ARD's `-NNN` suffix is ignored only when no row has the exact name. Found by the live `machine edit` test on 0.6.13, where every `Deco NNN` row matched every other.

## [0.6.13] - 2026-11-20

`machine edit` edits approved Bays properties (#531). No migration: the schema stays at 46.

### Added

- Verb `machine edit` and permission `bays_edit` (#531). It edits the Bays properties on an owner-approved list (LAN IP Address, Hostname, Device UIID, Has Paragon, Adobe email, Username, Avid System ID, Avid Activation ID, OS Version, Paragon License) with preview, then confirm. A property not on the list or a value of the wrong type fails the whole call; a blank or left-out value means no change; nothing is cleared; no row is created. Each applied row is audited as `machine_edit` with old and new values. `bays_edit` allows `machine edit` and nothing else. Typed client method `machine_edit`, and `t6.fleet.Fleet.edit`.

## [0.6.12] - 2026-11-19

The T6 Notion integration can edit without a bound Person (#509). No migration: the schema stays at 46.

### Fixed

- Notion edits made by the T6 Notion integration no longer need a bound Person (#509).

## [0.6.11] - 2026-11-19

The Concern scan stops flooding `llm run` (#504). No migration: the schema stays at 46.

### Changed

- The automatic PM Notes scan (`T6_CONCERN_AUTOSCAN`) runs once a day by default instead of every 15 minutes (`T6_CONCERN_AUTOSCAN_SECONDS` now defaults to `86400`); the first scan still runs at start (#504).
- A scan stops at the first `llm run` refusal and logs one counts-only line with the rule (`t6.server: concern scan stopped: llm run refused rule=...`). `concern scan` returns that refusal. Before, every PM Notes line was sent again on every scan: on Sixto 51k refused calls in two days (#504).
- The audit row of a refused `llm run` holds `refused_rule` and `failure` (fixed words, never text), so a refusal can be traced from the data (#504).

## [0.6.10] - 2026-11-19

A patch for the registry lock on Sixto (#502). Migration 46 takes the schema from 45 to 46; it is not forward-only, so the 0.6.9 image still opens the database.

### Fixed

- Opening a registry no longer holds the write lock for minutes on a large audit history (#502). Every open copies audit rows that have no activity row into `activity`, and its check scanned the whole `activity` table once per audit row, so each open took time quadratic in history and the API, `/health` and other workers waited on `database is locked`. Registry migration 46 adds the index `activity_by_audit` on `activity (audit_id)`; it only adds an index, so the previous release still opens the file. An index of that name made by hand before the upgrade is kept.

## [0.6.9] - 2026-11-17

A patch for the watchdog switch: `llm run` refused every watchdog extraction with rule `masking` because the template and schema held a Project name or code that is an ordinary word (#498). No migration: the schema stays at 45.

### Added

- `llm run` takes `dry_run: true` (#498): every check before the provider call runs, then the verb returns `done` with `output: null` and `dry_run: true`, and nothing is sent. A refusal is the same as a real call's. The audit record holds `dry_run: true`. An app calls it at start to prove its template and schema pass against the live registry.

### Changed

- The prompt template and output schema of `llm run`, and the question, criteria and state keys of `llm decide`, no longer refuse a registered name that is an ordinary word or a trivial term (a Project name under four letters or a plain Project word, a code or alias of one character), in any case. A name that is no ordinary word is still refused.
- A masking refusal says which part it came from, for example `Unresolved registered names in the output schema`, and never the word.
- Privacy: the `names` preset now leaves a Project name that is an ordinary word readable and hides an ordinary-word Project code only as written, capitalised or in capitals (what `strict` does for those two cases). The provider of `llm run`, `llm filter` and `llm decide` can now read ordinary-word Project names and codes that `names` used to hide. Person names, aliases, handles and IDs are unchanged, and masked reads still hide every registered name.
- `llm run` callers inside t6 with `masking_preset='names'` (transcribe translate, concern tasks) and the library defaults `t6.privacy.NAMES`, `Session` and `pseudonymize` are affected by this relaxation.
- `llm decide` also checks the state keys and the criteria keys, and `llm filter` names the part in its masking refusal (`... in the message`).

## [0.6.8] - 2026-11-17

One round of fixes for what the live checks and the reviews of 0.6.5 to 0.6.7 found. No migration: the schema stays at 45.

### Added

- Verb `text withhold` (#486, reader): up to 200 texts come back in order with labelled credential values replaced by the placeholder. An app calls it before it shows message text outside the chat it came from. Nothing is stored.
- `Client(require_unmasked=True)` (#488): the client sends `X-T6-Require-Unmasked`, and the API refuses every call with rule `unmasked` when the caller has neither `operator` nor the `unmasked` grant. For a caller that must never pass masked names on.
- `jo list`, `jo get` and `jo find` return `done` beside `status_group` and `active` (#477).
- Verb `bay list` (#421, reader): each Bay ID with the page ID of its Notion row.
- `deliverable edit` takes `--due` and `--air` and sets each when the Deliverable has none (#420). A date that is already set is moved with `deliverable delay`, as before.
- Verbs `queue failed` and `queue dismiss` (#414, operator): a failed internal job is dismissed with a reason and is never run again. For the webhook queue file on the edge host: `t6 admin queue failed` and `t6 admin queue dismiss`.
- One request log line for each API request (#373): time, identity, verb, outcome and duration, with no input, result, name or secret. The format is in `docs/deployment.md`.
- Masked reads load the client-name file again when it changes, with no restart (#369). `t6 health` shows the list under `api.privacy_registry`.
- `t6 health` shows `api.data_gaps.projects_without_name`: the count and page IDs of Projects with a blank name (#425). It does not fail the check.
- The image installs the `drives` extra, so the workbook import runs in the image (#417).

### Changed

- `approval request`, `project link-chat` and `project unlink-chat` reuse the open Approval of the same caller and input (#418). A retry makes no second Approval and no second operator card.
- A Package for the client ends at handoff (#422). `package accept` and `package reject` refuse it and say why.
- Every Drive read runs in one database transaction (t6-drives #19). `drive list` with 2,100 Drives went from 4.7 s to 0.6 s on a local file.
- `media migrate` with no External Links source refuses with rule `config` and says so (#424).

### Fixed

- `project hold` and `project resume` write the team's own Status option and never make a new one (#452). No match, or two, writes nothing and fails the job with the reason. `schema-check` reports `status_duplicates`.
- `llm run` with `masking_preset: strict` gives the withheld IDs back in the reply (#475).
- A Frame.io link made another way closes the pending "needs a show" operator card (#419).
- The abang relay asks a Person of kind `freelancer` as well as `vendor`. Since 0.6.7 a Contact can be made as a freelancer (#463), and `/abang` and `abang request` refused that Person.

## [0.6.7] - 2026-11-17

The second stage of moving the apps onto the t6 API (#426): every rule an app still applied itself is now answered by a verb, so an app imports only `t6.client` and the types in `t6.verbs`. Two migrations: the schema goes from 43 to 45. Neither is forward-only, so 0.6.6 can still open the file; take a backup first.

### Added

- `jo raise` for an app (#443, #459): the fields come as JSON, with no file upload over HTTP. With `message` (the header text, deadline text, post time, chat and message IDs, assignees, Project, Department text and number text) the API applies the Job Order rules itself and the preview returns the name, number, Department, deadline with its source and findings, the Telegram Key and the duplicate key. `no_project` raises a Job Order with no Project; `jo list` takes a `no_project` filter. `jo_id` is `null` in the preview and set at confirm. `T6_DEPARTMENT_ALIASES` (a JSON object) is merged over the default Department aliases.
- `llm run` (#444, #446): `examples` for few-shot turns, a `failure` class and `retry_after` on a refusal, the inputs the reports tasks need, and `provider_min_retention`, a map from a provider name to the lowest retention it may have. A provider with no entry keeps the zero-retention rule.
- Verb `llm decide` (#445, role `llm`): the Decisions provider answers match, maybe or no, with probabilities, for each candidate. Config key `decisions` (url, model, retention, `api_key_env`); its retention floor is the `decisions` entry of `provider_min_retention`. Names are masked.
- Verb `llm filter` (#474, role `llm`): one message is checked against a named filter rule and the answer is `pass`, `drop` or `unsure`. The rule text lives in t6-core; `job_order` is the first rule. Thresholds are pass at 0.5 and drop at 0.9, pass wins, and the optional config key `filters` overrides them per rule. The verdict is advice: on a refusal the caller goes on.
- Meeting Notes write verbs `meeting-notes create`, `append`, `trash` and `mark-sent`, and permission `minutes` (#447). `meeting-notes list` takes `latest`, `before` and `include_body`. The replies are masked for a caller without `unmasked` (#477).
- `person find` takes `mentions` (up to 50 at once), `suggest` and `prefer_telegram`; `person list` filters on T6 ID, `registered` and `kind` (#449, #450, #460).
- Person kind `freelancer`, beside staff, client and vendor (#463).
- `project resolve` takes `in_text` and finds a Project Code inside a text (#451).
- Verb `deliverable candidates` (reader): every Deliverable page of a Project (#441). Real names need `unmasked`.
- Reads return both ends of a date range, every related Project (`project_ids`), `prod_coor_supe_ids`, the time zone, `notion_project_code`, and the rule results an app needs: `status_key`, `status_group`, `active`, `health_flags`, `health_missing` and `health_rank` (#457, #458, #460). The Health results read the `Project Health` select (#479). `project flags` returns the date flags of one Project, or of several in one call with `projects` (#479). `jo get` returns the rule results before the Notion page exists (#477).
- One Concern list (#448): a Concern holds the brief text (`text`), a `source` and a nullable owner, and `closed_at`. `concern raise` and the PM Notes scan refuse a second open Concern with the same text on a Project (rule `duplicate`).
- Verb `concern observe` (#454, role `coordinator`): an app hands over the findings of one run and the API answers which are new, known or not seen, the open Concerns of each Project with their age, and what was closed since the last run. A run closes nothing unless the server has `T6_CONCERN_OBSERVE_CLOSES=1`.
- Verb `concern counts` (#455, reader): for each run date, how many Concerns were listed, sent, new and closed, their ages and sources. `concern record-message` takes `brief_date`.
- `t6 admin consumer-check` rule `t6-import` (#461): an app may import `t6.client` and `t6.verbs` and nothing else from the package.

### Changed

- Migration 44 rebuilds `concerns` (nullable owner, `text`, `source`, `text_key`, `closed_at`) and sets the source of old rows from how they were made. Migration 45 adds `concern_listings` and `brief_date`.
- `concern get` is masked for a caller without `unmasked`, like `concern list`.
- `llm run`'s `allow_projects` refuses a Project whose name or code is also a registered person (#477).

### Fixed

- Three read fixes (#458): a blank Health, the zone of a date, and the Project Code read from Notion.
- `concern observe` lists a closed Concern once, and stores `run_date` in ISO form (#477).

## [0.6.6] - 2026-11-16

The first stage of moving the apps onto the t6 API (#426): the gaps that stop the gateway, munki, the watchdog and reports from reading and writing through the API with no change for staff. No migration: the schema stays at 43.

### Added

- Permission `unmasked` (#429): a caller that holds it reads real client names in typed reads, and gets nothing else. A service gets it through `T6_API_SERVICE_PERMISSIONS`, for example `{"reports":["unmasked"]}`. The `mcp` service stays masked.
- Verbs `machine list` and `machine sync`, and permission `fleet` (#432). `machine list` (reader) returns the Bays rows in Notion as Machines with their fleet facts. `machine sync` (fleet) previews each Machine's matched row and each fact's old and new value, then writes on confirm. `fleet` allows `machine sync` and nothing else. Typed client methods `machine_list` and `machine_sync`.
- `t6 admin privacy-registry` (#433) builds the client-name file for masked reads from the Studios and Projects in Notion. It runs in the API container. The data-sources file needs a `studios` key.
- `departments` takes `include_misc` (CLI `--include-misc`) and then also returns MISC (#431).
- `project list` and `project get` return `project_health`, the `Project Health` select (#431). `health` is unchanged.
- `studio notices` returns `runs`, the description as text runs with a bold flag (#431).

### Changed

- `person set-telegram` on the API writes the Telegram ID to the Person's Notion Contact first, then the registry (#430), so a read of the Contact sees the write. A rejected Notion write answers `Refused` with rule `notion`; an unconfirmed one answers `Ambiguous`. In both cases the registry is not changed.
- `t6 admin consumer-check` also flags the credential names `NOTION_API_KEY` and `NOTION_TOKEN` (#427).
- `t6 departments` and `t6 person get` accept `--data-sources` and ignore it; the flag will be removed in a later release (#428).

### Removed

- The handlers of `person get` and `departments` that read Notion from the CLI process (#428). Both verbs are answered by the API only, as they already were in production.

## [0.6.5] - 2026-11-16

The MCP server gets a start command and a timeout setting, from the live MCP check on 0.6.4. No migration: the schema stays at 43.

### Added

- `t6 mcp` runs the read-only MCP server on stdio, the same server as `python -m t6.mcp` (#413); `docs/deployment.md` has an "MCP server" section with the settings, the `mcp` service binding and an example client entry.

### Fixed

- The MCP server's API client waits `T6_MCP_TIMEOUT_SECONDS` (default 60, not a fixed 10 s) so a slow read such as the unfiltered Project list is no longer cut off (#413); a value that is not a positive number stops the start, and a call that still times out returns `Unavailable` naming that setting. `Client()` elsewhere keeps 10 s.

## [0.6.4] - 2026-11-16

One fix from round 2 of the live test: a version stack made in Frame.io no longer leaves a failed job. No migration: the schema stays at 43.

### Fixed

- A `file.versioned` event that names a version stack no longer ends the `frameio.file_versioned` job failed with a 502 (#410): the Frame.io worker's `GET /frameio/files/<id>/location` answers a stack ID with the stack's folder, the stack and its `file_ids`, and the main worker records a Version with the stack for each file in a Deliverable's T6 folder that has none, and fills the stack on a Version that has none. An ID that is neither a file nor a stack ends the job as ignored.

## [0.6.3] - 2026-11-16

Fixes from the live test of 2026-11-16, and the `/project` card the owner asked for: a new Project is set up on one card, a tap is answered within about a second, the Frame.io review timer works with the VPS edge, a refused chat command gets an answer, and the Resolve reader uses the real tables. No migration: the schema stays at 43.

### Added

- `t6 admin drive import WORKBOOK` can apply the previewed import into the t6 database (#391): the preview report gains a `digest` (SHA-256 over the workbook, the `--log-sheet` and the `--projects` files given), and `--apply --preview FILE --actor NAME [--registry-db FILE]` writes the Drives, Assignments, custody events and exceptions only when the files still have that digest, then prints the counts written. A Project counts as resolved only when its Notion page is bound in the registry; any other name becomes an `unresolved project` exception. A second apply adds nothing. `--swing` is unchanged.

### Changed

- `/project` is quicker to use (#406): **New Project** on a New Projects page card now keeps the same card and asks for the code (`/project CODE` sets it and puts the Project in rotation; **Keep out of rotation, no code** closes it), and `/project TEXT` with no card waiting opens the Project whose code is TEXT or lists the Projects whose code contains TEXT or (from four characters) whose T6 ID ends with it, replacing the "No Project is waiting for a code" line.

### Fixed

- A refused `/link`, `/unlink` or `/abang` command now gets one short reply in its chat and topic (#400): the usage line, the verb's refusal reason, the coordinator-only line, or `No Person matches that mention.`. The API queues a `chat.reply` job per pushed command (a retried push queues one), and the main worker sends it when `T6_GATEWAY_URL` and `T6_GATEWAY_SECRET` are set; without them the job waits unclaimed. The answer to the gateway stays `rejected`.
- The main worker looks at the internal job table every 0.25 s while it waits for the next poll (#402), so an operator card changes within about a second of a tap, not 0.5 to 5 s later; the remote queue is still asked once per `T6_WORKER_POLL_SECONDS`.
- The Resolve project server reader uses the real tables (#394): `SM_Project` and `SM_ProjectFolder`, a project's folder from its `Folder` column, every named folder (including top-level ones) in the folder path, and one verified layout for Resolve majors 17 to 20 that `schema_for` matches on the major of the version (so `18.1.4` works).
- Review timers work with the VPS edge (#398): in edge mode the tailnet listener also serves `POST /review/timers` (same grant check, same queue file), so the Frame.io worker, which reaches only the queue server, no longer fails every `frameio.resolve_comment` job with `queue answered 404`. `/tap`, `/registry/*` and `/operator/*` still answer 404 on the edge. The `frameio.review_done` job is claimed from the queue server's `main` queue.
- `docs/operator-actions.md` rollout: the API listener accepts a pushed tap only from a secret bound to `gateway` in `T6_API_SECRETS` with `X-Telegram-User-Id` equal to `from.id`; the text no longer suggests `T6_TAP_SECRET` is enough (found live on 2026-11-16).
- A tap on an operator card that core has not yet confirmed as sent is carried out at once when it names its chat and `message_id`, instead of waiting about 35 s for the gateway's retries after a 503 `card_not_confirmed`, and the card is edited to its new text at once (#399).

## [0.6.2] - 2026-11-16

The owner's decisions of 2026-11-16: Drive custody verbs refuse a step that cannot follow the last one, the Drive verbs carry what the Drive web app needs, `project start` waits for its Approval, and a Deliverable can be edited. Two migrations: the schema goes from 41 to 43 (`drive_shipment_facts`, `project_starts`). Neither is forward-only, so 0.6.1 still opens the database.

### Added

- `deliverable edit` (#383): change the code or the episode of a Deliverable after it is defined (`operator`, preview and confirm, safe to repeat with the same idempotency key). A code may repeat inside a Project. Naming the current values is Done and writes nothing; a change writes Audit and Activity and queues the Notion sync, which now also writes the Deliverable code to the page name. The Frame.io folder keeps its old name. `project start` keeps its own rule.
- `t6.client.APIError` carries the HTTP status as `status` (#370), so a client can tell 5xx from 4xx without reading the message. It is None for an error raised before a request was made.
- `drive edit` changes every Drive fact (#370): description, brand, model, capacity, format, owner, ownership, purchase date, warranty and remarks, with the rules of `drive add`; a blank value clears. `drive get`, `drive list` and `drive find` return a `version` (a digest of the Drive's facts), and `drive edit` takes an optional `expected_version`, refused with rule `drive_changed` at preview and at confirm when it is not current. The `DriveStore` gains `set_facts`.
- `drive assign` takes `remarks`, stored on the Assignment and shown by `drive get` (#370).
- `drive unassign` ends an Assignment by its number alone (#370): `project` is optional when `assignment` is given, so an Assignment whose Project is text only (a workbook import) can be ended. Without `assignment`, `project` is still required.
- `drive list` carries each Drive's open Assignments (#370) as `open_assignments`: number, label, Project ID or text, role, start and holder (with the end, remarks and return date `drive get` shows), so the inventory needs no `drive get` per row.
- A shipment keeps the Drives' facts as they were at release (#370). `drive release` stores each Drive's facts, and `drive shipment` returns those, not the current ones; for a shipment made before this change it returns the current facts and `facts_at_release` is false. `drive release` also takes an optional `reason` (stored on the released events) and an optional free-text `recipient` (the person who took the Drives, stored on the shipment and shown by `drive shipment`). Migration 42 adds the `drive_shipment_facts` table (append-only); the `DriveStore` gains `shipment_facts`, and `release_shipment` takes `reason` and `recipient`.
- `t6 admin bay migrate --type OPTION` registers only the Notion Bays rows whose `Type` holds that option (#386). The Notion Bays database also holds machines that are not Bays; without `--type` every row is a Bay, as before.

### Changed

- Drive custody verbs refuse a step that cannot follow the last one (#378): `drive receive` is refused for a Drive in custody or retired, `drive release` for one released or retired (the whole release, naming every such Drive), `drive return` unless the Drive is released, and `drive retire` for one already retired. The refusal has rule `custody`, comes at preview and again at confirm in the write transaction, and names the Drive and its status. A retired Drive can no longer be received or returned through the verbs.
- `project start` waits for its Approval (#382): confirm only requests the Approval (gate `project start`) and stores the Start as waiting (`project_starts`, schema 43); it asks Frame.io for nothing and writes no Deliverable, Media location or Booking. Granting the Approval (`approval grant`, also the operator card) queues a `project_start.apply` job on the main queue; the main worker builds the plan again, then applies the Frame.io step and the t6 steps under the same derived keys, as the Person who confirmed. A changed plan, a refused step or a Frame.io failure records the Start as `failed` with the reason (Audit and Activity), and confirming again with the same key and token applies it again with no second Approval. A denied Approval ends the Start (Activity records the reason) and no longer blocks a new Start, and neither does a failed Start that applied no step (its granted Approval cannot be denied); while a Start waits, another is refused with rule `project_started`. The job result read and a retried confirm return the state: `waiting_for_approval`, `applying`, `applied`, `denied` or `failed`. The main worker needs no new setting: it asks the Frame.io worker through the `frameio` queue with its own grant. A Start that finished before this change is still read as started.

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
