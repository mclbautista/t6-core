# T6 domain glossary

Terms used in code, issues and tests. Use them one way only.

## Drive

A **physical external drive**: a portable drive or a powered desktop drive,
owned by T6 or by a Studio, that carries footage or deliverables for a show.
Modelled in `t6.drives`.

- A Drive is identified by its **serial**, normalized: notes in brackets are
  removed, then it is trimmed and upper-cased, so `" abc123 (old) "` and
  `"ABC123"` are the same Drive. One drive is recorded only once.
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
  Project and who logged it. Retired needs a reason. Events are never edited or
  deleted; a correction is a new event.
- **Status**: where a Drive stands (in custody, released or retired), always
  derived from the last custody event and never stored. No events, no status.
- **Assignment**: one stretch of a Drive's life on one Project: label, Project
  (a Notion page ID, or text when unresolved), role (master, backup, offline,
  swing, archive), start, end and remarks. No end means open, and "which drives
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
- **Register**: to create a Project in the registry. Registration is always
  explicit; nothing is registered or merged by matching a code or a name.
