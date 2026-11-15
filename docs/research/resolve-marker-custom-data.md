# Resolve timeline marker custom data: fact check (t6-core#185)

Research date: 2026-11-14. Scope: [issue #185](https://github.com/mclbautista/t6-core/issues/185),
a fact check under [spec #178](https://github.com/mclbautista/t6-core/issues/178).

**Status: done, with one gap.** Question 3 (reading markers without Resolve
open) is answered from the project server's database catalog. Questions 1 and 2
are answered by a scripted test on the 202 Mac Pro in a throwaway project. One
part of question 2, project backup and restore through the scripting API, could
not be tested: both calls returned `False`. See "Verdict".

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

## Questions 1 and 2: the scripted test (2026-11-14)

Where: the 202 Mac Pro, **DaVinci Resolve Studio 21.1.0.14** (`GetProductName()` =
"DaVinci Resolve Studio"). Resolve was already running with external scripting
already on (`System.Scripting.Mode = 2` in `config.dat`, unchanged: no
preference was changed by the test). The script connected with
`DaVinciResolveScript.scriptapp("Resolve")` over the local scripting interface.

Safety: the script aborted unless the current database was the local **disk**
database `T6-TEST-185` and that database held no project. It worked only on
projects named `T6-TEST-185*`. It never touched another database or the
project server (`activedb.conf` was `disk*:T6-TEST-185` and the project server
entry, unchanged). The test project was `T6-TEST-185-20261114`, with a new empty
timeline `T6-TEST-TL`. Cleanup: 3 test projects and 3 exported files were
deleted in total across two runs; the test database ended with 0 projects.

### Question 1: write and read marker custom data

| Call | Result |
| --- | --- |
| `timeline.AddMarker(0, "Blue", "t6 stamp", "t6-test", 1, "t6t_TEST_185_0001")` | `True` |
| `timeline.GetMarkers()` | `{0.0: {..., 'customData': 't6t_TEST_185_0001'}}`: one marker, custom data kept |
| `timeline.GetMarkerByCustomData("t6t_TEST_185_0001")` | returns the marker info. **Its keys are `color`, `customData`, `duration`, `name`, `note`: no frame position.** To find the position, use `GetMarkers()`. |
| `timeline.GetMarkerCustomData(offset)` | `'t6t_TEST_185_0001'` |
| `timeline.UpdateMarkerCustomData(offset, value)` | `True` |

**Yes.** Marker custom data can be written and read in Resolve Studio 21.1.0.14.
The frame position is an offset from the timeline start (the timeline's start
frame was 86400, which is 01:00:00:00 at 24 fps); offset 0 is the first frame.
An empty timeline (no clips) accepts the marker.

### Question 2: what survives

| Action | Calls | Stamp on the result |
| --- | --- | --- |
| Timeline duplicate | `timeline.DuplicateTimeline("T6-TEST-TL dup")` | **Same stamp, same position.** The duplicate carries the identical custom data. This is the "conflict" case: two timelines then claim one `t6t_` ID. |
| `.drt` export and import | `timeline.Export(path, resolve.EXPORT_DRT)` (17,802 bytes), `mediaPool.ImportTimelineFromFile(path, {"timelineName": ...})` | **Survives.** The imported timeline has the marker and the same custom data. (The imported timeline was named after the file, `tl`, not by the `timelineName` option: do not rely on the name.) |
| Project export and import | `projectManager.ExportProject(name, path, False)` (a `.drp`, 45,299 bytes), `projectManager.ImportProject(path, newName)` then `LoadProject(newName)` | **Survives.** The imported project's `T6-TEST-TL` timeline has the marker and the same custom data. |
| Project backup and restore, scripted | `projectManager.ArchiveProject(name, path.dra, False, False, False)` (tried with the project open and closed), `projectManager.RestoreProject(path, newName)` (tried with the `.drp` and with a `.dra`) | **Could not be tested.** `ArchiveProject` returned `False` and wrote no file, and `RestoreProject` returned `False`. Cause not found. A person can test restore in the Resolve UI (Project Manager), or we find out why the calls return `False`. The `.drp` export and import above is the closest result. |
| Copy to the project server | not tested | Not approved and not tested. It needs a NEW empty test database on the live project server. The client (21.1.0) is newer than the server (20.0.0), which may prompt a database upgrade. |

## Verdict

**Is the first-frame marker stamp viable? Yes, with conditions, as long as
Resolve is running when T6 reads it.**

1. **Viable:** the `t6t_` ID can be written with `AddMarker(..., customData)` on
   the first frame, found with `GetMarkerByCustomData`, and it survives timeline
   export and import (`.drt`) and project export and import (`.drp`). Editors
   can rename timelines freely, because the ID is in the marker and not the name.
2. **Duplicate = conflict (condition):** a duplicated timeline keeps the same
   stamp, so T6 must treat two timelines with the same `t6t_` ID in one project
   as a conflict, and re-stamp the copy with a new ID. This is expected, not a bug.
3. **No plain SQL reads (condition):** the custom data is not in a plain column
   (see question 3). Reading it needs Resolve running (scripting API) or the bay
   scanner (#18), or decoding the blob. Not tested here: whether the custom
   data is readable inside the blob (a separate approval is needed to read a
   test database's blob).
4. **Gap:** scripted project backup and restore is unverified (both calls
   returned `False`). Copy to the project server is untested.

If conditions 2 or 3 are not acceptable, the proposed alternative is to keep
the `t6t_` ID in a T6-owned table keyed by the timeline's Resolve UUID, with the
timeline name as a hint only. That is a proposal, not a finding.

**Cyril's view (2026-11-15):** the T6-owned table keyed by the timeline's Resolve
UUID "sounds like a good idea". Nothing is decided or built from this note.

**Open before that design can be chosen (not tested):** whether the timeline's
Resolve UUID (the scripting API's `Timeline.GetUniqueId()`) stays the same, or
changes, after a duplicate, a `.drt` export and import, a `.drp` project export
and import, and a restore. If an import or a restore gives the timeline a NEW
UUID, a table keyed by UUID loses the link, and T6 would need the marker stamp
(or another hint such as the name) to re-link it. A test with a throwaway
project, like the one above, would answer this.
