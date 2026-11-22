# Resolve timeline marker custom data: fact check (t6-core#185)

Research date: 2026-11-14. Scope: [issue #185](https://github.com/mclbautista/t6-core/issues/185),
a fact check under [spec #178](https://github.com/mclbautista/t6-core/issues/178).

**Status: updated 2026-11-22 (#579), see the last two sections.** The 2026-11-14 text below is kept as written.

**Status on 2026-11-14: partly done.** Question 3 (reading markers without Resolve open) is
answered from the project server's database catalog. Questions 1 and 2 need
Resolve's scripting API and a test project, and are NOT answered yet. The
first-frame marker stamp is therefore **not** declared viable or not viable
here. See "Still open" and "Verdict so far".

## How this was checked

- Date, host and approval: 2026-11-14, Cyril's written approval for a
  read-only inspection of the Resolve project server on Sixto.
- Read-only only: process and file checks, then `SELECT` on catalog tables
  (`information_schema`, `pg_catalog`) of ONE database. The session was set
  `default_transaction_read_only=on`. No project content was read, no project
  name appears in this note or was written anywhere, and nothing was changed,
  locked, dumped, backed up or restored.
- The database is called "database 1 of 4" here: the largest of the four
  non-template databases on the server, picked by size so that no name is needed.

## The setup (what is true today)

| Item | Finding |
| --- | --- |
| Project server | DaVinci Resolve Project Server **20.0.0**, installed on Sixto |
| Database | Native PostgreSQL **13.4** (EnterpriseDB build), not a container. Data in `/Library/PostgreSQL/13/data`, started at boot by the launchd job `com.edb.launchd.postgresql-13`, port 5432 |
| Databases | 4 non-template databases (sizes in MB, largest first: 27145, 22, 7, 7) |
| Use | Live: editors' machines hold open connections (5 sessions at the time of the check). Treat it as production. |
| Resolve Studio on Sixto | **Not installed.** Only the project server app. No `Developer/Scripting` folder, so `DaVinciResolveScript` and `fusionscript` are not available on Sixto. |
| Resolve on the 202 Mac Pro | DaVinci Resolve **21.1.0** at `/Applications/DaVinci Resolve`, scripting folder present (`DaVinciResolveScript.py`, `fusionscript.so`, README, Examples). Whether it is the Studio edition is not shown by the app's `Info.plist`; a scripting call (`GetProductName()`) will say. Resolve was not running. |
| Version gap | The client on the Mac Pro (21.1.0) is newer than the project server (20.0.0). A newer client may ask to upgrade the server's database format. This matters for any test that copies a project to the project server. |

## Question 3: how can markers be read without Resolve open?

**Answer: not as plain columns.** In database 1 of 4 (schema `public`, 136
tables, all with `uuid` ids):

- The only column named like a marker is `Sm2TiItem.MarkersBA`, type `bytea`
  (a serialized byte array; "BA" is Resolve's name for byte array). It belongs
  to a timeline item (a clip placed on a track). The only other match is
  `SmTimelineFilter.FilterMarkerSubMode` (a filter setting, not marker data).
- No table has "marker" in its name. No column is named for custom data.
- Timelines are in `Sm2Timeline` (about 34,000 rows by the planner estimate),
  which is linked to `Sm2Sequence` and the track tables `Sm2TiTrack` (about
  267,000 rows) and `Sm2TiItem` (about 10,400,000 rows). Their columns are
  mostly `bytea` blobs: `Sm2Timeline.FieldsBlob`, `Sm2Timeline.ImportExportMetadataBA`,
  `Sm2Sequence.FieldsBlob`, `Sm2Sequence.PinsBA`, `Sm2Sequence.UIElementsState`,
  `Sm2TiItem.FieldsBlob`, `Sm2TiItem.MarkersBA`, `Sm2TiTrack.FieldsBlob`.
- A timeline has a plain `Name` and a `Sm2Timeline_id` (uuid), and plain
  creation and modification times. The first-frame marker would live in a blob.

So marker custom data, if it is stored at all, sits inside a binary blob, most
likely `Sm2TiItem.MarkersBA` for markers on items, and somewhere in the
`FieldsBlob` / `PinsBA` columns for markers on the timeline itself. This
catalog check cannot say which blob holds a timeline marker, or whether the
custom data string is stored there in a readable form, because that needs
reading blob content (project data), which was out of scope.

**What this means for T6 (not a verdict on the stamp):** reading a timeline's
`t6t_` marker without Resolve open would mean decoding Resolve's undocumented
binary format from PostgreSQL. That is fragile and tied to Resolve's version.
The expected answer in #185 stands: reading depends on Resolve running (its
scripting API) or on the bay scanner (#18), not on plain SQL.

## Still open (needs Resolve's scripting API)

1. Can marker custom data be written and read through the scripting API
   (`Timeline.AddMarker(frameId, color, name, note, duration, customData)`,
   `Timeline.GetMarkerByCustomData(customData)`, `Timeline.GetMarkerCustomData(frameId)`)
   in Resolve Studio 21.1.0?
2. Does the custom data survive: a `.drt` export and import; a project backup
   and restore; copying the project to the project server; duplicating the
   timeline (and does the duplicate keep the same stamp, which would be a
   "conflict")?
3. (Follow-up to question 3) In a test project on a PostgreSQL database, is the
   custom data readable inside the marker blob, so a scanner could read it
   without Resolve?

A test plan with a throwaway project is in the thread for Cyril's approval; it
uses only a new project named `T6-TEST-185-<date>` and never an existing project
or the live project server.

## Verdict so far

Not yet. Question 3 is answered (not plain columns). Whether the first-frame
marker stamp is viable depends on questions 1 and 2. If the scripting API
cannot read the stamp back after an export or a restore, the proposed
alternative is to keep the `t6t_` ID in the timeline's name or in a Project
database table that T6 owns, and not in Resolve's data. That is a proposal, not
a finding, and it is not decided here.

## Results 2026-11-14 and 2026-11-15 (from the #185 comments)

Scripted test on the 202 Mac Pro, Resolve Studio 21.1.0.14, throwaway projects in a new local disk database.

- `AddMarker` with customData, `GetMarkerByCustomData`, `GetMarkerCustomData` and `UpdateMarkerCustomData` work.
- The stamp survives `.drt` export and import and `.drp` project export and import.
- `DuplicateTimeline` keeps the same stamp: a conflict T6 must detect and re-stamp.
- `Timeline.GetUniqueId()` is stable across reloads but changes after a duplicate, a `.drt` import and a `.drp` import. It can only be a hint, never the key.
- Verdict: the first-frame marker stamp is viable, with those conditions.

## Results 2026-11-22 (#579)

Scripted test on the 202 Mac Pro (macOS 15.6.1), `GetProductName()` "DaVinci Resolve Studio", `GetVersionString()` 21.1.0.14. Owner's written OK, throwaway projects only. The scripting API cannot create a database, so a new disk database entry `T6-TEST-579` was added to Resolve's database list (config backed up and restored afterwards); every call checked `GetCurrentDatabase()` first. No existing database was opened or listed, and no upgrade prompt appeared.

- **frameId is an offset from the timeline start.** On a 24 fps timeline starting at 01:00:00:00 (`GetStartFrame()` 86400), `AddMarker(0, ...)` sits on the first frame; frameId 86400 is a separate marker one hour in. `AddMarker(-1, ...)` returns False.
- **Saving persists the stamp.** `SaveProject()` returned True; after `CloseProject` and `LoadProject` the marker kept its customData, including a value set by `UpdateMarkerCustomData`.
- **A script runs against an open Resolve** (default project, then the test project) without trouble. Not tested with an editor's real project open.
- **Project server write and read: not tested.** It needs a database on the project server; only the live one is reachable, and the 21.1 client against the 20.0 server may offer a format upgrade, so it was left alone.
- **Project open by another user: not tested** (no safe second client). Resolve's log shows it takes a project lock on load and releases it on close.
- **Restore: not tested.** `ArchiveProject(name, path, False, False, False)` returned False with a file or a directory path. **Calling `ArchiveProject(name, path)` with two arguments made Resolve's process disappear; never call it that way.** With no archive, `RestoreProject` was not run.
- `ExportProject(name, path.drp, False)` and `ImportProject` returned True, and the imported timeline kept its stamp.

Open: writing a project on the project server, a project locked by another user, and restore. Until then the Resolve worker treats every load or write failure as a final job failure with its reason.
