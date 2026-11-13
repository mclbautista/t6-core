# T6 domain glossary

Terms used in code, issues and tests. Use them one way only.

## Drive

A **physical external drive**: a portable drive or a powered desktop drive,
owned by T6 or by a Studio, that carries footage or deliverables for a show.
Modelled in `t6.drives`.

- A **Drive ID** is a permanent opaque identifier, `t6d_` plus a UUID4 hex
  suffix, issued at creation. Equality and all store references use this ID.
- An **accepted serial** is an optional, correctable fact, unique across
  Drives. Normalization removes bracketed notes, trims and upper-cases.
  Exact serial lookup matches accepted values only, never labels or descriptions.
- A **suspected serial** is an explicitly uncertain candidate. It can conflict
  with other accepted or suspected values. Accepting it requires explicit
  promotion and the accepted-serial uniqueness check.
- A Drive without an accepted serial is still a full Drive, not provisional.
  It needs some meaningful identifying detail. Serial edits preserve its ID,
  Assignments, custody history, shipment membership and derived status.
- **Needs review** and a general **review note** represent other doubts; they
  impose no additional workflow restrictions. There is no automatic merge.
- A Drive is not a Notion entity; it has no Notion row. Its owner is T6 or a
  Studio (a Notion page ID).
- "Drive" never means Google Drive. Always write **Google Drive** in full for
  the cloud storage service.
- The **OCF Drive Planner** keeps its name. It plans storage for original
  camera files and is not about Drive records.
- **DriveStore**: the interface where Drive records live. The custodian's web
  app (spec: mclbautista/t6-drives#1) implements it; `t6.drives.testing.InMemoryDriveStore` is the fake for
  tests.
- **Custody event**: one entry in a Drive's append-only custody log: received,
  released, returned or retired, with date, the Contact or Studio involved, the
  Project and who logged it. The date and the party are blank when they were
  never recorded. Retired needs a reason. Events are never edited or
  deleted; a correction is a new event.
- **Shipment**: one handover of several Drives: released events that share a
  shipment ID. A **manifest** is a view over those events: each permanent Drive
  ID, accepted and separately suspected serial, available label, description,
  capacity and review state, the recipient, date and who logged it. The core gives the
  data; the web app prints it.
- **Status**: where a Drive stands (in custody, released or retired), always
  derived from the last custody event and never stored. No events, no status.
- **Assignment**: one stretch of a Drive's life on one Project: label, Project
  (a Notion page ID, or text when unresolved), role (master, backup, offline,
  swing, archive), start, end and remarks. Role and start are blank when they
  were never recorded. No end means open, and "which drives
  are out for show X" is a query over open Assignments. A reused Drive gets a new
  Assignment; old ones stay as its history.

## Contact matching

One policy matches a mention to a Contact (`t6.lookup`):

- **Honorifics**: 16 words (ate, kuya, sir, maam, mam, madam, mr, mrs, ms,
  miss, atty, engr, direk, dir, tito, tita) are removed from the start of a
  name only, always keeping at least one word.
- **Order**: Telegram user ID, then Telegram handle, then alias or name, then
  fuzzy. The Project team is searched first, then all Contacts. Two matches at
  one step are a tie and go to a person.
- **Fuzzy**: rapidfuzz WRatio on names and aliases. It decides at 90 or more
  with a lead of 8 over the runner-up, otherwise the result needs review.
- **Suggestions**: `suggest_contacts` gives up to 3 candidates at WRatio 60 or
  more, only for a person to choose from. It never decides, links or writes.

## Departments

Job Orders belong to a **Department**. Notion's `Department` options are the
truth: `t6.notion.departments.get_departments` reads them live, with option
IDs. t6-core owns the display order (TOC, AE, OE, COOR, COLOR, VFX, SOUND,
ADMIN, then any other Department A to Z, then none) and the alias `TO` for
`TOC`. MISC is not a Department. A token that matches no live option is an
`UnknownDepartment`, never a guess.

## Studio time and deadlines

`t6.domain.studio_time` and `t6.domain.deadlines`.

- **Manila**: Asia/Manila is the one studio zone (`MANILA`), a fixed UTC+8
  because it has no daylight saving. Every date shown or compared uses it.
- **Shift calendar**: three shift reports a day, at 05:00, 11:30 and 19:00
  (`SHIFT_REPORT_SLOTS`). Each covers the time since the previous one went out.
- **Deadline**: `deadline(phrase, posted_at, assignees)`. A stated time is used
  as is; a date with no time means 23:59. "End of shift", "within shift" and
  "asap" use the latest assignee end of shift at or after posting; a morning
  end of shift with a stated date rolls to the next day. When no assignee has a
  fixed shift the deadline is 23:59 with a `shift_fallback` finding. "Asap"
  adds an `asap` finding. A missing year takes the closest year. Hedges and
  fillers are stripped. A deadline before posting is kept and flagged
  `deadline_past`. Anything else is `deadline_unparsed`, never a guess. The
  base time is the intake posting time.

## Job Order rules

`t6.domain.job_order_rules`.

- **Header**: `<PROJECT> - <DEPT> - JO #<N>`. The number part accepts `JO #74`,
  `JO No. 15`, `#0155`, `JOB ORDER 27`, `JO01` and `JOB ORDER - 033`; digits
  are kept as posted. A `JO CODE:` label and a stray leading backslash are
  dropped. The **canonical Name** rebuilds the header; the first line is the
  fallback.
- **Numbering key**: one JO counter per Project, shared by every Department.
  The duplicate key is `<project page ID>|<number>`.
- **JO ID** (in reports): `<code>#<number>`, one "#"; several Projects joined
  with "/"; no Project is "no project"; no number is "#?".
- **Telegram Key**: `<chat_id>:<message_id>`.
- **Deliverable codes**: FTR (feature), TLR (trailer), TSR (teaser), SHR
  (short), EpXX and EpXX-XX (episodes).
- **Formatter defaults**: Watermark is "none"; Link Settings travels with Mode
  of Transfer; MFG shuttle drives are `MFG Shuttle 01` to `04`, never 05.
- **Status groups**: not started (Needs Intake Review, Pending), started (In
  progress, Needs Completion Review, Done), cancelled. Active means not Done
  and not Cancelled.
- **Health flags** that show: Blocked, Delayed, Urgent.

## Project status rules

`t6.domain.project_rules`, over `Project`. Status and Health match by their
words, ignoring emoji, case and spaces.

- **Status gate**: In-Progress, In-Progress TVC and Airing are *in progress*;
  Standby and Archiving are *on hold*; Leads, Lead, Negotiations and Proposal
  Made are *incoming* (to refine later with the CRM). Any other Status is
  dropped. Archived and Lost are never snapshotted.
- **Date flags**: In-Progress and TVC flag a blank or overdue Playdate and
  Submission Deadline; Airing flags blank ones only; Standby none; Archiving a
  blank or overdue Archival Deadline; Incoming an overdue Playdate or one less
  than 30 days away. Overdue is strictly before today in Manila.
- **Missing roles**: for in-progress Projects, each of the eight roles
  (editor, assistant editor, online editor, colorist, sound tech, post
  coordinator, director, cinematographer) with nobody assigned.
- **Health**: a blank Health is flagged. Order: Critical, At Risk, On Track,
  Incoming, other, blank.
- **Code display**: the code in capitals, or "CODE (Name)" when several
  Projects share it; the name when there is no code.

## Consumer contract

The rules every app follows to use t6-core (scope, boundary, `t6.domain`,
Notion IDs, CI enforcement, version pins) are recorded in
`docs/adr/0002-consumer-contract.md`.

## Project

A **client show** that T6 works on. Modelled in `t6.domain` (the descriptive
fields, read from Notion) and `t6.registry` (identity).

- **Project registry**: the single T6-wide record of which Projects exist. It
  owns each Project's ID, code, rotation, bindings and audit history. Notion
  owns the descriptive fields. See
  `docs/adr/0001-project-registry-owns-project-identity.md`.
- **Project ID** (T6 Project ID): the permanent, opaque identifier the
  registry issues at registration, written `t6p_...`. It never changes and is
  never reused. It is not the Notion page ID.
- **Project Code**: the short human-facing code of a Project. Optional. It is
  not identity: codes are reused once a Project leaves rotation.
- **Rotation**: whether a Project currently holds its code. A Project is **in
  rotation** or **out of rotation**, set explicitly and never derived from
  production Status. A new Project starts out of rotation.
- **Historical Project**: a Project out of rotation. It keeps its code on
  record, and a bare-code lookup does not return it while another Project
  holds the code. It is found by its T6 Project ID.
- **Hold** (a code): only a Project in rotation holds its code, and only one
  Project across T6 holds a given code at a time. A Project out of rotation
  keeps its code on record without holding it, so another Project can take it.
- **Unclassified**: the rotation of an imported Project that nobody has
  reviewed yet. It blocks its recorded code until someone **classifies** it as
  in or out of rotation.
- **Binding**: the link from a Project to one external page that stands for
  it, today a Notion page. A page is bound to at most one Project. The
  **active** binding is the Project's current page; a replaced page stays as a
  **legacy** binding and still points to the same Project.
- **Migration**: registering the Projects that existed before the registry,
  one per Notion page, unclassified. It is previewed first, never matches by
  code or name, and keeps every ID on a rerun.
- **Classify**: to decide an unclassified Project is in or out of rotation.
  Only a person decides; Status never does.
- **Legacy reference**: a page ID that still names a Project in a Job Order,
  Deliverable, External Link, Drive Assignment or event. It resolves to the
  Project through its binding, including a replaced page's binding.
- **Projection**: the registry-owned values a Notion page displays: T6 ID,
  Project Code and Rotation. Notion shows them; it does not own them. An edit
  there is put back.
- **Sync**: showing an accepted registry change in Notion. It happens after
  the change is committed and its failure never undoes the change.
- **Reconcile**: to compare a Notion page as it is now with the registry and
  act on the difference. Reconciliation changes no Project.
- **Review item**: something reconciliation found that a person must look at:
  an unbound page, an edited projection, or an unavailable page.
- **Register**: to create a Project in the registry. Registration is always
  explicit; nothing is registered or merged by matching a code or a name.

## Frame.io

- **Frame.io worker**: the one process that holds the Frame.io token. It
  serves the `frameio` queue. Nothing else in T6 has Frame.io credentials.
- **Request** (Frame.io): one typed question or step for the Frame.io worker,
  sent as a job on the `frameio` queue. Its **result** is `Ok` or `Failed`.
  A **Frame.io project** is Frame.io's own object, never a T6 Project.
- **Caller**: any part of T6 that asks the Frame.io worker and waits for the
  result: the main worker, t6-notion-reports.
- **Review message**: the one Telegram message to the review chat that says a
  person has finished commenting on a Frame.io file. A **review timer** is
  kept per (file, commenting person) and restarts on each of their comments.
- **Sign-in**: a person signing in to Adobe so the Frame.io worker has a
  token again. The **sign-in alert** is the Telegram message with the link.
