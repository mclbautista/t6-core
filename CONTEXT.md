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
  app implements it; `t6.drives.testing.InMemoryDriveStore` is the fake for
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
- **Sign-in**: a person signing in to Adobe so the Frame.io worker has a
  token again. The **sign-in alert** is the Telegram message with the link.
