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
