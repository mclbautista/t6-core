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
- A Drive is not a Notion entity; it has no Notion row. Its owner is T6, a
  Studio (a Notion page ID) or unknown. Unknown is not T6; the workbook
  importers record it because a sheet row gives no evidence of ownership.
- "Drive" never means Google Drive. Always write **Google Drive** in full for
  the cloud storage service.
- The **OCF Drive Planner** keeps its name. It plans storage for original
  camera files and is not about Drive records.
- **DriveStore**: the interface where Drive records live. The t6 database
  implements it as `t6.drives.sqlite.SqliteDriveStore`; the custodian's web
  app (spec: mclbautista/t6-drives#1) is a consumer, and
  `t6.drives.testing.InMemoryDriveStore` is the fake for tests. Both pass the
  same contract tests.
- **Source row**: the workbook, sheet and row an imported Drive came from. The
  store keeps it for traceability, and a repeated import of a row changes
  nothing.
- **Exception queue**: the durable, private store of workbook rows the importer
  found unsafe, unreadable or unattached, each with its source row and reason.
  Nothing in it is guessed or merged. Its evidence stays out of the repository.
- **Custody event**: one entry in a Drive's append-only custody log: received,
  released, returned or retired, with date, the Contact or Studio involved, the
  Project and who logged it. The date and the party are blank when they were
  never recorded. Retired needs a reason. Events are never edited or
  deleted; a correction is a new event.
  The custody verbs refuse a step that cannot follow the status: receive when in
  custody or retired, release when released or retired, return unless released,
  retire when retired.
- **Shipment**: one handover of several Drives: released events that share a
  shipment ID. A **manifest** is a view over those events: each permanent Drive
  ID, accepted and separately suspected serial, available label, description,
  capacity and review state, the recipient, date and who logged it. The core gives the
  data; the web app prints it.
- **Status**: where a Drive stands (in custody, released or retired), always
  derived from the last custody event and never stored. No events, no status.
- **Inventory**: the Drives with their derived status, read with `drive list`. Its
  filters combine with AND: text, exact accepted serial, needs review, a Project with an
  open Assignment, and status. A Drive with no custody events shows the status `unknown`.
- **Assignment**: one stretch of a Drive's life on one Project: label, Project
  (a registered T6 Project ID, or text when unresolved), role (master, backup,
  offline, swing, archive), start, end and remarks. The label, role and start
  are blank when they were never recorded. No end means open, and "which drives
  are out for show X" is a query over open Assignments. A reused Drive gets a new
  Assignment; old ones stay as its history.
- **Assignment number**: the place of an Assignment in its Drive's list, counted
  from 1 in the order they were added. It never changes, because an Assignment is
  never removed. `drive unassign` picks the Assignment to end by it when several are open.
- **Swing drive**: a Drive with a `swing` Assignment, lent to a Project, to a
  Person (the holder), or to both. A swing held by a Person alone has no Project:
  an Assignment with no Project must name a holder, and a Drive cannot have two
  open swings for the same holder. The Assignment may have a **return date**, the
  day it is due back. `swing return` ends the swing and clears the date;
  `swing unassign` ends it and keeps the date in history. A swing is **overdue**
  when it is still open and its return date is before today (Manila time). The
  Notion Swing Drives database is imported once and then retired. One Notion row
  is one Drive, keyed by its page ID only: it gets a provisional ID, no serial,
  unknown ownership and its label from Name, and is never matched by label to
  another Drive. A row in use with one Collaborator that is a registered Person
  (a Contacts page bound to a Person) gets an open swing held by that Person, with
  no Project. Any other holder question goes to the exception queue with the
  page ID.

### Drive recording and reloads

- **Movement**: one IN or OUT of one Drive, or an unresolved label from a
  loaded row, on a date, with the person as typed, recorder when known, Place,
  Project and origin. It is append-only; a correction is a new entry.
- **Movement log**: the searchable history of Movements, including loaded
  movements that cannot be tied with certainty to a Drive.
- **Capture**: a recording-channel post kept before reading or matching it,
  with its chat and message IDs, sender Telegram user ID, post time, text and
  photo. A Capture may be text-only (no photo). It is the source of the Movements
  made from its Items, including Guesses waiting on Approval.
- **Item**: one named line of a Capture's caption (a short name such as "MC7 D21"
  or a label), on the first line after IN/OUT or on a following line. It is read as
  written: "RM D07 (Master 02 and 03)" is one Item.
- **Guess**: a ranked candidate Drive for an Item, with a confidence and a
  one-line reason. Guess 1 records a Movement at once, marked **to review** until
  the custodian approves it; a different pick or None appends a correction.
- **Short name**: an Item's text learned as a name for a Drive and its Project,
  with the number of custodian confirmations and the last confirmation time.
- **Reference photo**: the newest approved Capture photo of a Drive. A later
  text-only Approval keeps the existing Reference photo.
- **Count line**: a caption line that is only a number and a kind ("48 FD", "50
  Portable drives"). It is kept on the Capture and is never an Item.
- **Inbox**: the Items waiting on the custodian's Approval. Every Item of a kept
  Capture is in the Inbox until it is settled; Count lines never are. A Capture
  that needs the custodian is an Inbox item, not an exception: exceptions are
  sheet and data problems only. A Capture whose caption has a problem (no IN or
  OUT, or an OUT without `R:`) waits for its poster to edit it and is not in the Inbox.
- **Approval**: the custodian's decision on one Item: a Drive (picked or typed),
  `new` (create the Drive), `not_a_drive` (close the Item) or `none` (leave it
  waiting). Approving guess 1 clears **to review** on its existing Movement;
  picking another Drive appends a correction. A Movement keeps the Project's T6
  Post Coordinator Person IDs as they were at the move, apart from the recipient.
- **Recording channel**: TRANSMITTAL, configured by Telegram chat ID plus topic ID
  (the whole chat when no topic is configured),
  whose drive posts are recording input under [ADR 0009](docs/adr/0009-recording-channel.md).
  QR labels are not used for recording.
- **Reload**: a complete replacement of Drive data from the source sheets,
  including Assignments, custody events, shipments, the Movement log,
  exceptions and Captures, preserving each accepted serial's Drive ID. Short
  names and Reference photos stay only for preserved Drive IDs; the accuracy
  log spans Reloads.
- **Reload report**: the account of a Reload's rows read, Drives, Project uses,
  matched and unmatched movements, exceptions and unmatched Project names.
- **Practice mode**: the period before Cutover when recordings are practice
  records, reads and replies say "Practice", and the next Reload wipes them.
- **Cutover**: the one-way switch to t6 as the only Drive record, made by the
  deployment setting `T6_DRIVE_LIVE=true` (ADR 0010): the end of Practice mode,
  no further Reloads, and the source sheets made read-only by the custodian. The
  last Reload before the deploy is the final load; starting reloads nothing.
- **Place**: a Drive's in-office storage location: TABLE, CABINET, SHELF, BOX,
  or none when unrecorded. An editor taking a Drive is an OUT, even when the
  editor stays in the office.

## Bays and Bookings

A **Bay** (`t6b_`) is a bookable place, today a row of the Notion Bays
database. t6 owns its identity only: the Notion Bays row, bound by page ID, keeps the typed fields.
Migration registers a Bay per unbound row, never matches by name, and is safe to
run twice.
`bay list` (reader, also an MCP read tool) answers each registered Bay's `t6b_` ID with the page
ID of its Notion row; the name stays in Notion.
A Machine's record is the same kind of row (see Fleet).

A **Booking** (`t6g_`) is one Person in one Bay on one Project from a start to
an end, both with a time zone, kept in Manila time. `--shift day|night` takes
the times from the Person's shift pattern. Booking verbs (`bay book`,
`booking move`, `booking extend`, `booking cancel`) preview, then confirm.
- **Overlap**: a Booking that overlaps an active Booking in the same Bay is
  refused unless forced. A forced Booking is recorded as **forced**.
- **Cancelled**: a cancelled Booking stays on record and no longer counts for
  overlap.
- **Booking calendar sync**: every Booking change queues one `booking.calendar_sync`
  internal job that writes the Booking's event to its Bay's Google Calendar (one
  event per Booking, its ID derived from the Booking ID). The title is the
  Project's name; the description names the Person and the Booking ID. With no
  calendar configured the job finishes and says so.
- **Inbound calendar edit**: a change to a Booking event in Google Calendar. The
  `google.calendar_changed` job (queued by the edge from a Google notification,
  which names no event) lists the changed events and runs `booking move`, `booking extend` or
  `booking cancel`. Google names no editor, so with the real adapter the edit is accepted as made by
  a `bookings` holder: the actor is the internal service actor `google-calendar` (not a Person, not
  usable through the API) and the Activity says the change was made in Google Calendar and that
  Google does not name the editor. When an editor is known, a Person with `bookings` (found by the
  email on their Notion Contact) is the actor and anyone else is reverted. A clash is **reverted**:
  the event is written back from the Booking with a note, and the Booking does not change. An event
  that already equals its Booking is our own write coming back and is ignored. **Calendar sharing is
  the permission check for inbound edits**, and the daily reconcile job keeps it true.
- **Notification channel**: the Google channel that sends changes of one Bay
  calendar to the edge. The daily `google.renew_channels` job replaces it before it expires, and it is also the **daily catch-up**: it lists every Bay calendar from its sync token and applies what it finds, so a lost notification is caught within a day. A changed channel token queues a run at the next worker start.
- **Calendar sharing**: who can edit a Bay calendar follows the `bookings` permission.
  `person grant` or `person revoke` of `bookings` queues a `booking.calendar_sharing` job in the
  same transaction; it gives the Person (found by the email on their Notion Contact) edit
  (`writer`) or view (`reader`) on every Bay calendar. Every other active staff Person has view.
  The daily `google.reconcile_sharing` job compares each calendar's ACL with t6 and fixes
  drift (t6 wins), logging each fix by a short hash. A writer whose email is no Person's (added by hand), and any group, domain or public writer entry, is lowered to view. The reconcile reads emails only for active staff, and one failed email lookup is counted and skipped. `person classify` and `person rebind` queue the sharing job too. Manager entries (the calendar owner and the
  service account) are never changed. Revoke sets view; it does not
  delete the entry.

## Bay scans

Decided in ADR 0008 (#18). The report model, the ingest and the read are built (#376, #377); the scan script and the drift
report follow (#379, #380).

- A **local copy** is a Resolve project held in a bay's local disk database rather than on the project server.
- A **bay scan report** is what one bay's nightly job sends to t6: the scan time, the Resolve version and the bay's
  local copies. It is tied to a Bay (`t6b_`) through the machine serial and the Machine to Bays mapping, never by name.
  t6 keeps the latest one per Bay.
  `bay scan report` posts one. It needs the `bay_scan` permission, carried only by the `bay-scanner` service (the Mac mini),
  which may run no other verb. A serial that maps to no Bay is refused with rule `no_bay`; a repeat or older scan changes
  nothing. `bay scan list [<bay>]` shows each Bay's last report as `fresh`, `stale` (scanned more than 3 days ago) or
  `no_report`, which is never the same as "no local copies". Its replies hold project names, so they are masked like other
  reads.
- **Drift** is a local copy that does not match the project server by the definition #374 settles. The drift report
  only reports; nothing is changed.

## Timelines

A **Timeline** (`t6t_`) is a DaVinci Resolve sequence timeline known by its
**stamp**: the `t6t_` ID in the custom data of a marker at the timeline's
first frame. The stamp is the identity; the Resolve name is only a label, so
editors may rename and back up freely. T6 writes the stamp by code, through the
Resolve worker; no person types it. This is the one Resolve write T6 makes
today (ADR 0002, rule 10; its proposed amendment allows more).
`timeline link` without `--stamp` creates the stamp: the preview shows the new
`t6t_` ID (the same one for the same key), and the confirm records exactly it
as **pending** and queues a `resolve.stamp` job. The Resolve worker writes the
marker and reads it back; a success marks the Timeline **stamped** (verified),
and a failure marks it **failed** with the reason (`queue failed` shows the
job). A job is tried once: if the worker stops mid-job its lease runs out, the
job is dead and the Timeline failed. Linking a failed Timeline again (new key)
queues it again, with or without `--stamp` (by its label, it keeps its stamp);
it takes the name given as its label, in Audit. `timeline list`
shows each Timeline's stamp state. Timelines linked before T6 wrote stamps
count as stamped. A Timeline that already
carries a stamp is not given a new one (pass `--stamp`), except that at confirm a timeline
carrying exactly the stamp the preview showed is accepted; a retried confirm with the same key returns the first result. `timeline list` refuses a
Project that is not registered.

- **Resolve worker**: a fleet Resolve agent: a process on a Mac with Resolve
  Studio that holds Resolve scripting access and claims Resolve jobs through
  the t6 API; it never opens the t6 database. Any fleet Resolve agent may write
  Resolve, only through registered verbs, behind a person's confirm and an
  automatic restore point, and every write records its host, Resolve version
  and restore point (ADR 0002 rule 10, amendment proposed, awaiting owner
  acceptance). The red lines never lift: no change or delete of a user marker
  or timeline without the owner's confirm, no delete of a Resolve project or
  database, no `ArchiveProject`, no direct write to the project server's
  PostgreSQL. Today it writes only the stamp marker (add, update, read back)
  and never deletes a marker. Before writing it refuses a timeline that carries a different stamp,
  or a stamp already on another timeline of the project. It closes each
  Resolve project it loads (Resolve locks a loaded project until then) and
  makes the database that was current before current again.

- A Timeline records its `t6t_` ID (the stamp), its Resolve project (the
  Project's active resolve-project Media location, on the project server) and
  the label. The copy on the project server is the **canonical copy**; copies
  in other Resolve projects are not recorded.
- **One Timeline per stamp.** Linking a stamp already linked to another Project
  or Resolve project is refused, and so is a stamp found on two timelines of the
  Resolve project (a duplicate keeps the same stamp). The fix is re-stamping
  by code: `timeline restamp` (previewed) gives the copy that is not the
  canonical Timeline a new `t6t_` ID, records it as a pending Timeline, and the
  Resolve worker replaces the stamp on it. The canonical Timeline is refused by
  its label or by the Resolve unique id its stamp was verified on, and the
  worker refuses that id again at write time; the Timeline's Resolve project
  must still be active. A failed re-stamp linked again still replaces the
  duplicated stamp. Linking the same stamp again for the same Resolve project
  records nothing new, whatever the name now is (a failed one aside, above).
- A Resolve unique id is not the key: it changes on duplicate and import.
- A **Version** may name its source Timeline (`version add --timeline`); the
  Timeline must belong to the Deliverable's Project.

## Media locations

A **Media location** (`t6m_`) is a typed link from a Project or Deliverable (its
**owner**) to where material lives. It replaces the Notion External Links
database; its migration maps rows by page ID only and sends rows it cannot
classify to a review list.

- **Kind**: what the `ref` points to: drive (a `t6d_` Drive ID), timeline (a
  `t6t_` Timeline ID), nas-path, frameio-project, frameio-folder,
  resolve-project or instagantt-project (the `ref` is the Instagantt project ID
  as an Instagantt export gives it, free text, role working). An Instagantt
  project is not a Timeline: a Timeline is a Resolve timeline with a stamp.
- **Role**: what the location is for: master, backup, review, working or
  frameio-project. These are not Drive Assignment roles.
- A Frame.io project, and an Instagantt project, belongs to one Project at a
  time. A frameio-folder belongs
  to a Deliverable with role review.
- A file that lands in a Deliverable's frameio-folder records a Version (`t6r_`) from the
  `file.versioned` or upload event, run by the main worker, once per event. A file in
  no T6 folder is ignored.
- **Unlink** makes a Media location inactive; it is never deleted. Linking the
  same owner, kind, ref and role again reactivates it. Its history is
  append-only: link and unlink, with actor and time.

## Project Start

**Project Start** is one verb, `project start`, with a previewed plan. It is a
preview by job: the verb returns a job id, the main worker builds the plan, and
the job's result is the Preview with its signed single-use token (read with
`project start --job <id>`). Confirm (`--confirm <token>`, same inputs and key)
requests the Approval and stores the Start as waiting; a changed input is
refused. The other steps wait for the Approval: when it is granted a job on the
main queue applies the **t6 steps** and the Frame.io step as the Person who
confirmed, after building the plan again. A denied Approval ends the Start and
applies nothing; a new Start (new key) may begin. A changed plan, a refused
step or a Frame.io failure makes the Start `failed` with the reason (Audit and
Activity); confirming again with the same key and token applies it again with
no second Approval. A failed Start that applied no step does not block a new
Start (new key). The job result read and a retried confirm return the state
(`waiting_for_approval`, `applying`, `applied`, `denied`, `failed`). One Start
of a Project at a time. Role: operator, and the `bookings` permission when the Start asks
for Bookings.

- **t6 steps**, each created once under a key derived from the Start's key (the
  Approval, gate `project start`, state requested, a card goes out like any
  Approval, is made at confirm; the rest when it is granted): the Deliverables with the milestone template of the Project Type
  (the Type is read once; a Type without a template refuses the plan), the
  Media locations of the Project, and the initial Bookings. Each step goes
  through the store that owns it, so its rules (overlap, one Frame.io project
  per Project) apply. A retry after a failed step resumes without repeating
  the steps already done.
- **Manual steps** the plan lists and does not do: creating the NAS folders, the
  Resolve project and the Resolve timelines. Their stamps are written by code
  when each timeline is linked (`timeline link`).
  The apply records each in Activity (`project_start_manual`) with `project_start`.
- **Frame.io step**: the apply asks the Frame.io worker (the only token holder)
  through the `frameio` queue for one Frame.io project and one folder per
  Deliverable, and records them as Media locations: the project as the
  Project's frameio-project (the `frameio project link` rules, role
  frameio-project), and one frameio-folder with role review per Deliverable.
  The Frame.io project is named by the Project's name (read from Notion by the
  plan) and each folder by its Deliverable code, in the configured T6 Frame.io
  account. Confirm asks first: a Frame.io failure refuses the confirm and leaves
  t6 unchanged. The step is safe to repeat. The Frame.io worker uses the
  Frame.io project the Project already holds, else the one in the account with
  exactly that name that no other Project holds, else makes one; it never
  guesses between two. A folder is found by exact name or made. So a repeated
  confirm, or a retry after a crash between the Frame.io call and the t6
  record, creates nothing new. No share is made (a share always needs a
  person's yes), nothing is uploaded (the editor uploads), and version stacks
  form as cuts arrive.
- **Plan order**: approval, NAS folders, Frame.io project and per-Deliverable
  folder, Resolve project, Resolve timelines, Media locations, milestone
  template, Bookings.
- A Deliverable code the Project already has is refused (rule `deliverable_code`)
  at preview and at confirm, so a Start never gives one Frame.io folder two
  owners; the Deliverables a Start's own key made do not count. The Deliverable
  store itself does not forbid a repeated code.
- A Deliverable code may repeat inside a Project (`deliverable define` allows it).
  `deliverable edit` changes the code or the episode of a Deliverable after it is
  defined (operator, preview and confirm, safe to repeat with the same key). It
  names only the fields to change. It also gives a Deliverable its first due or air
  date (`--due`, `--air`), each only when the Deliverable has none; if it already has
  that date the verb refuses (rule `date`) and names `deliverable delay`, which keeps
  its rule for moving an existing date. A blank
  code is refused, as is an archived Project; naming the values the Deliverable
  already has is Done and writes nothing. A change writes Audit and Activity and
  syncs the Notion page name. The Frame.io folder is not renamed: the Media
  location points at it by ID, so the link stays valid and the folder keeps its
  old name. `project start` keeps its own rule above.
- A Project is started once: a second Start, or a Start of an archived Project,
  is refused. Frame.io Media locations are refused in the `media` input of a
  Start: they come from the Frame.io step only.

## Floor reads

Three derived reads over Deliverables, Milestones, Packages and dates, per Project
or studio-wide. Nothing is stored; dates are Asia/Manila days. A Deliverable is
ready on the earlier of its `delivered` Milestone date and the day its delivery
Package (Package kind `delivery`) became kasado. Other Package kinds are
handoffs between people and never make a Deliverable ready for air.

A Package for the recipient `client` (the delivery Package `deliverable deliver` makes
and a prepared one) ends at handoff: the client is not a Person, so no one can accept
or reject it, and `package accept` and `package reject` refuse it with a reason that
says so. Only a Package whose recipient is a Person is accepted or rejected.

**Kasado** (ready) is one rule, used by `package get` and by these reads (#178):
every blocking item of the Package's checklist is ticked. Every spec starts with
zero blocking items, so a Package with none is kasado from the day it was
prepared; with blocking items it became kasado on the day the last one was
ticked (Asia/Manila). A rejected Package is never kasado.

Checklist items have a **kind** (ADR 0011, proposed): **mise** (preparation
before the work), **pipeline** (the work steps) or **handoff** (what holds before
the Package goes to the next person). A **gate** stops the next stage: open
blocking mise items stop the pipeline verbs; open blocking pipeline items stop
the handoff.

- **Bangko**: a Deliverable ready at least the configured margin (default 3 days)
  before its air date.
- **Hand-to-mouth**: a Deliverable ready within the configured margin (default 1
  day) before its air date, with no buffer.
- **Sabit**: a Deliverable that missed its date (a recorded miss). A date moved
  with a reason is delayed, not sabit. A Deliverable missed twice is listed once,
  with its latest miss.

## Transcripts

A **Transcript** (`t6x_`) is the record of transcribing the audio in one active
nas-path Media location, with at most one Deliverable of the same Project. It
holds settings, a state (running, done or failed), progress and the last run
summary (clips done, failed and skipped, audio hours, estimated cost). It
keeps no audio and no transcript text. Running again on the same Media location
reuses the Transcript.

- **NAS share**: a mounted network volume (`/Volumes/<name>`) that the transcribe
  worker may read, named in that worker's own configuration. A folder outside
  every NAS share is refused (`outside_shares`) before anything is read.
- **Transcript history**: the append-only entries of a Transcript: created,
  re-run, failed, translated and speakers changed, each with actor and time. A
  speakers entry keeps the speaker map as given. Each entry shows in Activity
  for the Project.

## Person

A **Person** (`t6n_`) is someone t6 knows by a Contacts page, with one **kind**:
staff, freelancer, client or vendor.

- **Staff**: works for the studio and has the staff role (below).
- **Freelancer**: a person who works on Projects for the studio and is not staff.
  A freelancer does not have the staff role.
  The abang relay asks a freelancer or a vendor.
- **Client**: a person at the client of a Project.
- **Vendor**: a person at an outside supplier.

## Delivery links and the staff role

A **Delivery link** is how a Delivery reaches the client: its carrier, one of a
Drive, a Frame.io share or a transfer link, kept with its reference. When the
carrier is a Frame.io share, a client's view of it is recorded in the Project's
Activity.

The **staff role** is the role table's lowest write role: an active Person whose
kind is staff. Freelancers, clients, vendors, inactive Persons, services and internal actors
do not have it. Verbs that need no named permission use it (`transcript create`,
`transcript speakers`, `transcript translate`, `frameio project link`); the
`reader` role is for reads only, never for a write.

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
`TOC`. MISC is not a Department, unless a reader asks for it (`departments --include-misc`). A token that matches no live option is an
`UnknownDepartment`, never a guess.

## Studio time and deadlines

`t6.domain.studio_time` and `t6.domain.deadlines`.

- **Manila**: Asia/Manila is the one studio zone (`MANILA`), a fixed UTC+8
  because it has no daylight saving. Every date shown or compared uses it.
- **Shift calendar**: three **turnover reports** a day, at 05:00, 11:30 and 19:00
  (`SHIFT_REPORT_SLOTS`). Each covers the time since the previous one went out.
  "Turnover report" is the one name for all three ("shift report" and
  "end-of-shift report" are not used); the slot titles stay.
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
- **Formatter layout** (`t6.domain.job_order_format`): canonical header, a date
  line computed from the date, core fields in order with the writer's labels,
  Watermark "none" by default, Status and Pending blank, free text last. What
  cannot be settled (header, Title, Task, Purpose, Assigned, deliverable code,
  Link Settings, MFG shuttle range) is a finding.
- **Filter rule** (`t6.domain.message_filter`): a name, a question, the `match`,
  `maybe` and `no` criteria, and the thresholds (`pass` at 0.5 for `match`, `drop`
  at 0.9 for `no`, otherwise `unsure`). The `job_order` rule asks whether a message
  is a request for work: chatter, questions and replies that ask for no work are
  not Job Orders. The texts sit in `t6.domain.job_order_rules`.
- **Status groups**: not started (On Hold, Needs Intake Review, Pending), started (In
  progress, Needs Completion Review, Done), cancelled. Active means not Done
  and not Cancelled.
- **Health**: the Notion Health formula, read by its words, ignoring emoji.
  Closed (Done or Cancelled), Blocked (Blocker has text, or Status is On
  Hold), Delayed (past the Deadline), Urgent (0 to 5 hours to the Deadline),
  Advanced (2 days or more to the Deadline), On Track (anything else). Every
  turnover report line starts with its Health icon: ✅ Closed, 🔴 Blocked,
  ⚠️ Delayed, 🔥 Urgent, ⚡️ Advanced, 🔵 On Track.
- **On Hold**: a not-started Status (Notion To-do group). Its Health is
  Blocked.

## Project status rules

`t6.domain.project_rules`, over `Project`. Status and Health match by their
words, ignoring emoji, case and spaces.

- **Status gate**: In-Progress, In-Progress TVC and Airing are *in progress*;
  Standby and Archiving are *on hold*; Leads, Lead, Negotiations, Proposal
  Made and Onboarding are *incoming* (see CRM). Any other Status is dropped. Archived and Lost are never snapshotted.
- **Date flags**: In-Progress and TVC flag a blank or overdue Playdate and
  Submission Deadline; Airing flags blank ones only; Standby none; Archiving a
  blank or overdue Archival Deadline; Incoming an overdue Playdate or one less
  than 30 days away. Overdue is strictly before today in Manila.
- **Missing roles**: for in-progress Projects, each of the eight roles
  (editor, assistant editor, online editor, colorist, sound tech, post
  coordinator, director, cinematographer) with nobody assigned.
- **Health**: a blank Project Health is flagged. Order: Critical, At Risk, On Track,
  Incoming, other, blank.
- **Archiving rule**: a Project is *archiving* when its Status is Archiving or
  its Stage (the Notion `Stage` select of the production, not the CRM stage) is
  Archiving, For Drives Turnover, Waiting for Payment or Festival Rounds,
  whatever the Status.
- **PSR group**: the PSR section a Project is shown in, first match wins:
  archiving (see the archiving rule), incoming (an incoming Status), then
  Critical, At Risk or On Track by Project Health. A blank or unknown Health
  counts as On Track.
- **Reviewed every PSR**: In-Progress, Airing and every archiving Project are
  shown at every PSR, changed or not. In-Progress TVC is not.
- **Unknown Status**: a Status that is none of the 13 live options
  (In-Progress, Leads, Onboarding, Standby, In-Progress TVC, Proposal Made,
  Negotiations, Operations, Archiving, Archived, Lost, Malayo Pa, Airing).
  Archived and Lost are known; a blank Status, and the old spelling Lead (which
  the status gate still reads as incoming), are unknown.
- **Code display**: the code in capitals, or "CODE (Name)" when several
  Projects share it; the name when there is no code.

## CRM

The Leads side of a Project, before it is In-Progress (spec #705).

- **Lead**: a Project in a CRM stage other than Lost. It is a Project, not a
  separate record.
- **CRM stage**: Leads, Negotiations, Proposal Made, Onboarding or Lost
  (`t6.domain.CrmStage`). The t6 database owns it; the Notion Status shows it.
  The stage options are read and written by their saved Notion option ID (the
  `projects` `named_options` in the schema snapshot), so a renamed option is
  still found. Nothing blocks a move: any stage to any other, Lost from any
  stage, Lost back to Leads reopens. In-Progress ends the CRM part.
- **Stage history**: one append-only row per change (who, when, from, to), in
  `project_stage_history`. The newest row is the current stage; the days in
  stage count from its time. A change by a known Person is accepted and shows
  in Activity as from and to; an unknown editor is flagged as before.
- **Lead Owner**: the Person named in the Project's Lead Owner relation.
- **Lead contact**: a client person on the Project: its Delegate Producer, Line
  Producer or Prod Coor/Supe.

- **Default owner**: the Person named by `T6_CRM_DEFAULT_OWNER_PERSON_ID`. A Lead
  with a blank Lead Owner, or one who cannot be messaged, goes to them, and they
  take a snoozed person's items when no Post Producer can.

- **Snooze**: a person pauses their own CRM DMs through a date (Manila, inclusive,
  at most 30 days). Their items go to the Post Producers, then the default owner.

`lead list` reads the open Leads with stage, Lead Owner, Studio, the Lead
contacts and days in stage.

## Fleet

A **Machine** is a computer in the fleet; the Bays row is its record. `t6.fleet`
owns the Machine to Bays mapping and the fleet facts: Serial Number, Munki
Manifest, Ring, Munki Client Version, Last Check-in and OS Version.

- **Rings**: testing, pilot, production. Software is imported into testing,
  verified on the pilot machines, then promoted to production.
- **Role groups**: nine manifests (toc, sound, production, online, dit,
  warroom, coordination-admin, client-facing, interns-temporary). A machine
  manifest includes exactly one. The manifest chain of a machine is itself,
  its role group, `all` and `site_default`.
- **`machine list`** reads every Bays row as a Machine with its fleet facts (role `reader`). It is
  not the Bays of the registry that Bookings use (`t6b_`, #421). **`machine sync`** is the Bays
  write-back as a verb: it previews each Machine's matched row and each fact's old and new value,
  then on confirm writes them. It needs the `fleet` permission, which grants nothing else.
- **`machine edit`** edits other Bays properties through the API, so an app never holds a Notion token
  or a Bays property name. Only the owner-approved list is editable: LAN IP Address, Hostname, Device
  UIID, Has Paragon, Adobe email, Username, Avid System ID, Avid Activation ID, OS Version and
  Paragon License (#531). A property not on it, or a value of the wrong type, fails the whole call.
  It previews each matched row's old and new values and writes on confirm; a blank or left-out value
  means no change, and nothing is cleared. Each applied row is audited (`machine_edit`, with old and
  new values). It needs the `bays_edit` permission, which grants nothing else. Hostname is also a
  row match key, so a caller finds the row by its current name and the new Hostname applies after.
- **Bays write-back**: a machine matches a row by its names against the row's
  Hostname and Name, ignoring case and spaces; a row with exactly that name wins, and ARD's `-NNN` suffix is ignored only when no row has it. It writes
  only to one unique row, never creates a row, never clears a fact, and is a
  dry run unless applied.
- **Staleness**: an observation older than 3600 seconds, or with no time, is
  stale. Its observed facts (client version, OS version, last check-in) are
  not written.

## Failed jobs

A job that used all its attempts is failed (dead, in the internal queue). It is never run again. An operator
**dismisses** it with a reason: the job keeps its row, failure and dedupe key, stops counting as failed, and the
dismissal is audited. There are two queues (ADR 0006): the internal jobs in the t6 database (verbs `queue failed` and
`queue dismiss`) and the webhook jobs in the VPS edge queue file (`t6 admin queue`, run on the edge host, with the
dismissal kept in that file). Failed jobs are not re-run: the operator fixes the data another way.

## Concern

A Concern is shared information. It is a waiting-on record, and it is also the
data that makes up the text of the brief (term: **Concern text**), so the
studio can count the Concerns raised at every PSR and see how they age and
close. An app keeps no Concern list of its own. Each Concern has a source:
`person`, `pm_notes_scan`, `date_rule`, `flag_rule`, `pm_notes_read` or
`leave`. A Concern may have no owner and may wait on nobody. None of these
sources is chat text (ADR 0007).

Terms fixed by the owner on 2026-11-23 (Concerns and JO turnover grill). Where
a term describes behaviour that is decided but not yet built, it says so.

- **PSR**: the Monday production status review meeting, and only that.
- **MOM**: the minutes of the PSR (the Notion minutes page and its send). Not
  "minutes draft". Paused from 2026-11-23 until the owner turns it back on.
- **Wednesday brief**: the whole weekly send at Wednesday 10:00 Manila. It
  replaces the Saturday 12:00 send ("PSR brief", "Saturday briefing"). Being
  built. It has two parts:
  - **Master brief**: a DM to each Person with `master_brief` on. It holds
    every Project, every Open Concern and every pending close. It replaces the
    "PSR digest".
  - **Coordinator brief**: the coordinators' Projects, posted to Sixto's
    channel topic. It replaces the per-coordinator "PSR briefing" DM. It holds
    no Concerns until the Concern DM setting says `coordinators` (it says
    `master` at rollout).
- **Open Concerns**: the brief section that lists every open Concern. It
  replaces "Raise at PSR".
- **Closing after PSR**: the brief section that lists the pending closes.
- **Concern line**: one bullet in either section: `Project: text · last
  evidence <date> · owner <name> · waiting on <party> · open N days`.
- **PM Notes**: the Projects text the coordinators write. A **dated line** is
  one under an "As of <date>" header; only dated lines are read.
- **Autoscan**: the run that reads new dated lines, archives them in the PM
  Notes history and matches them to Concerns. It runs daily at 21:00 Manila
  and not at API start; one catch-up runs at start when a run completed before the most recent 21:00
  and none since; the first start after the upgrade runs none. The Monday 21:00 run, after the PSR, is the one that
  settles pending closes.
- **PM Notes history**: every dated line the autoscan has read, one row per
  Project, date and line text, with first seen, last seen and removed (the
  first run the line was absent). It is kept for good; an edited line shows as
  removed plus a new line. Persons with `master_brief` on and the Project's
  coordinators read it
  (verb `project notes-history`).
- **Close**: a person's close of a Concern through Sixto (verb `resolve`). It
  becomes a pending close. Undo (verb `reopen`) cancels it. Over the API both
  follow the verb's role only.
- **Closer**: a Person who triages Concerns in the Concerns sheet. Access is Google
  sharing of the sheet; there is no Closer setting in t6.
- **Concerns sheet export**: a CSV with one row per Concern that is not
  closed (`Open` or `Closing after PSR`), with the actions a Closer may choose
  for it. The API pushes it to the VPS server after each completed 21:00
  autoscan, and the server serves the latest file only to the read token.
  Spec #732.
- **Sheet Submit**: the Concerns sheet's button. The VPS verifies its signed
  POST and queues one job; the main worker applies each changed row with the
  Concern verbs as actor `sheet:<email>`, keeps a result per row (shown as
  `last result` until the next Submit for that Concern) and pushes a fresh
  export. Spec #732.
- **Done line**: a dated line that says a Concern is finished (the scan's
  `settles`). It becomes a pending close. It replaces "announce".
- **Pending close**: a Close or Done line waiting for the next Monday 21:00
  autoscan. That run applies it at its end unless a veto came first. A close
  made after Monday 08:30 Manila waits for the following Monday. A failed run
  applies nothing; the next run that completes applies what it left. A run
  decides only the Projects whose lines it all read. A
  Concern with a pending close is still open, but `concern list` shows it only
  in its `pending` list.
- **Veto**: a dated line, dated between the pending close and that Monday's
  autoscan, that the scan reads as continuing the Concern. It cancels the
  pending close, and dated after the Concern's as-of date. The MOM does not
  count.
- **Data close**: an immediate close from data (a Job Order done, an approval
  granted, a milestone met, a Project date filled in). It replaces "settle".
- **Snooze**, **quiet** and **expired** are not used. Snooze is removed and
  every snoozed Concern was reopened; quiet and expired are legacy states that
  do nothing.

## Schedule snapshot

Modelled in `t6.schedule`; layout in [schedule-monthly-calendar.md](docs/schedule-monthly-calendar.md) (spec #694).

- **ScheduleSnapshot**: `ScheduleSnapshot/v1`, the versioned result of reading one
  tab of a client's calendar-style Sheet: source scope, source revision, layout
  version, read coverage, Groups and Candidates. The planner (#695) consumes it.
  It is never written back to the Sheet and holds no Instagantt IDs.
- **Source scope**: the document and tab a snapshot came from. One tab is one
  dated version of the whole schedule.
- **Group**: one independently read part of a tab (a month block). Its status is
  `complete`, `held` (a problem confined to it; it carries no Candidates) or
  `incomplete` (parts of it were not read).
- **Hold**: a typed exception that stops a Group or the year being guessed, such
  as `ambiguous_year`, `invalid_header` or `weekday_mismatch`.
- **Candidate**: one task the sheet shows: snapshot-local `candidate_key`, title,
  inclusive ISO `start` and `due`, optional tentative marker and source ID, and
  provenance. Its cell address is provenance, not identity across revisions.
- **Read coverage**: the ranges read against the tab's full extent. Incomplete
  coverage (a short read, an unreported size, no merge list) is whole-source
  uncertainty: every Group that is not held becomes `incomplete`. It is never
  evidence that a task is absent.
- **Day state**: what was read at a day-number cell: `read`, `blank`, `unread` or
  `invalid`. Only `read` lets an empty day mean "no task here".
- **Unsupported layout**: the typed result for a tab that is not a supported
  layout; it carries a diagnostic and no Candidates.
- **Month block**, **week band**, **day pair**: the layout's parts: a month name
  with its weekday header, the rows from one day-number row to the next, and the
  two columns that make one weekday.
- **Non-task**: availability ("NA:"), note or break text; never a Candidate.

## Consumer contract

The rules every app follows to use t6-core (scope, boundary, the API,
Notion IDs, CI enforcement, version pins) are recorded in
[ADR 0002](docs/adr/0002-consumer-contract.md). The related decisions are
[credential ownership](docs/adr/0003-one-owner-per-credential.md),
[entity identity, relations and history](docs/adr/0004-entity-identity-relations-and-history.md),
[studio verbs](docs/adr/0005-studio-verbs.md),
[the two queues](docs/adr/0006-two-queues.md), and
[no chat text](docs/adr/0007-no-chat-text.md).

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
  Deliverable, Drive Assignment or event. It resolves to the
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
  A **Frame.io project** is Frame.io's own object, never a T6 **Project**: this file
  writes "Frame.io project" for Frame.io's and "Project" for T6's, and a Project holds at most one
  Frame.io project through its frameio-project Media location.
- **Caller**: any part of T6 that asks the Frame.io worker and waits for the
  result: the main worker, the API (Project Start confirm), t6-notion-reports.
  Almost every request is a read; `CreateProjectFolders` is the one **write**
  request (a Frame.io project and its folders, never a share).
- **Review message**: the one Telegram message to the review chat that says a
  person has finished commenting on a Frame.io file. A **review timer** is
  kept per (file, commenting person) and restarts on each of their comments.
- **Unlinked Frame.io activity**: review activity on a Frame.io project that
  no Project holds (no active frameio-project Media location). The main worker
  **holds** it in the t6 database (the Frame.io worker never opens it) and posts one
  operator card per Frame.io project, a picker of Projects that a coordinator
  (`permission:coordinator`) answers. Tapping a Project runs `frameio project link`; the
  held activity is then **released**, each event once, whatever linked the project
  (the card, the verb, or anything else). It is never dropped and no comment text is
  stored with it. Skipping the card leaves it held and the next event posts a new card.
  When the Frame.io project is linked another way, the pending card is closed as linked elsewhere and edited in Telegram.
- **Link Frame.io project**: a button on the `/project` card, for any active staff
  Person (a staff Person who is not an operator gets the card with only this
  button). It lists the Frame.io projects of the configured T6 Frame.io account that
  no Project holds, most recently updated first, five to a page, with a typed search.
  A second Project choosing a Frame.io project another Project holds is refused, naming the holder.
- **Sign-in**: a person signing in to Adobe so the Frame.io worker has a
  token again. The **sign-in alert** is the Telegram message with the link.

## T6 database and IDs

The **t6 database** is the SQLite store for T6 entities. Its numbered schema
migrations run on open; each records whether it is forward-only. Each migration
must remain readable by the previous release or be marked forward-only. Migrations 2 through 4 are compatible with the previous release. The bindings
column named `project_id` holds IDs of any entity kind. Take a backup before upgrading.

A **T6 ID** is an opaque, permanent identifier whose prefix identifies its kind.
The prefixes are Project `t6p_`, Deliverable `t6v_`, Package `t6k_`, Version
`t6r_`, Timeline `t6t_`, Person `t6n_`, Bay `t6b_`, Booking `t6g_`, Media
location `t6m_`, Concern `t6c_`, Approval `t6a_`, Job Order `t6j_`, Drive
`t6d_`, Milestone `t6s_`, Chat link `t6h_`, Transcript `t6x_`, and Abang relay `t6q_`. Project
IDs retain their existing 24-hex-digit format; the others use 32 lowercase hex
digits. Drive IDs retain their UUID4 format. Existing IDs are never rewritten.
A binding links an external page to an entity ID; replaced pages remain legacy
bindings. Project bindings and registry behavior continue as before.

## Notion reads

Every Notion read goes through the reader layer (`t6.notion.source.NotionSource`),
never directly through `NotionClient` or `open_notion` (enforced by the AST
guard in `tests/test_notion_read_guard.py`).

- **Default freshness is `mirror`**: callers omit `freshness` or pass
  `freshness="mirror"` so routine reads can be served from the local mirror once
  enabled.
- **Explicit `live` reads**: callers pass `freshness="live"` only by explicit
  choice — for write confirmations and pre-write checks, live schema or option
  lookups, unmirrored page block bodies (`list_block_children`), webhook
  reconciliation, one-off migrations, or explicit live reads like `jo get`.
- **Allowlisted reasons**: every `freshness="live"` call site outside
  `t6.notion.source` must be listed in `LIVE_READ_ALLOWLIST` in
  `tests/test_notion_read_guard.py` with a one-line reason explaining why live
  Notion is required.
