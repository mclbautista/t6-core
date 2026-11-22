# Bay Resolve local library: fact check (t6-core#374)

Research date: 2026-11-22. Scope: [issue #374](https://github.com/mclbautista/t6-core/issues/374),
the facts that [ADR 0008](../adr/0008-bay-scanner-boundary.md) marks "(confirm in #374)".

## How this was checked

- Host and approval: one pilot-ring bay (Mac, macOS 26.2), with the owner's
  written approval for a read-only check on one testing-ring bay.
- Route: SSH from the Mac mini with the existing fleet key, `BatchMode` (no
  prompt). No configuration was changed on either machine.
- Read-only: Resolve was not running before or after (`pgrep`), `lsof` showed
  no open handles, and the library was copied with the bay as the rsync
  source. Only the copy on the Mac mini was opened, with `sqlite3 -readonly`.
  The copy was deleted afterwards.
- No project, folder, client or person name appears in this note.

## Findings

| Question | Answer |
|---|---|
| Resolve version | 20.3.1 (app `Info.plist`). Versions on other bays are not checked. |
| Location | `~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Resolve Project Library/Resolve Projects/Users/<user>/`. There is no folder named "Resolve Disk Database". |
| Layout | `User.db`, `Configs/`, `ProjectMetadataCache/Metadata.db`, and `Projects/<folders>/<project>/Project.db` (plus `Batch Renders/*.xml`). Folders are plain directories, nested; `SM_ProjectFolder` is empty. On this bay: 12 projects, about 540 MB, one Resolve user with a library. |
| Format | Every `.db` is plain SQLite 3 (written by 3.33.0). A `Project.db` has 146 tables. |
| Lock files when closed | None: no lock, journal, `-wal` or `-shm` files. |
| Project identity | `SM_Project.SM_Project_id`, a UUID. `Metadata.db` `project_metadata.uniqueId` holds the same UUID (11 of 11 cache rows), but the cache had 11 rows for 12 projects. |
| Name | `SM_Project.ProjectName`, equal to the directory name in all 12. |
| Modified time | `SM_Project.LastModTimeInSecs` (unix seconds). Timelines have `ModTimeInSecs` and `CreateTimeInSecs`. |
| Timeline count | `count(*)` of `Sm2Timeline` in `Project.db`. The cache's `numTimelines` was stale for 1 of 11, so the cache is not a source. |
| Timeline stamps (`t6t_`) | Not found in any of 1,379,776 blobs (zlib blobs also inflated and searched). This bay probably holds no stamped timeline, so this proves nothing. [#579](https://github.com/mclbautista/t6-core/issues/579) used the scripting API only, so blob readability needs a kept stamped control project (owner approval). Marker data sits in blobs, as on the project server ([#185](resolve-marker-custom-data.md)). |
| Tailnet | **Not on the tailnet.** No `tailscale` command, app, daemon or `100.x` interface. |

## What this means for ADR 0008

- Decision 1 and 3 hold. The scanner reads `Project.db` files from a copy and
  never trusts `Metadata.db`. "Resolve is open" is a match on the Resolve
  app's executable path, not a loose `pgrep Resolve`.
- Decision 6: what can honestly be compared is (a) identity by project UUID,
  if the project server stores the same `SM_Project_id` (not yet checked),
  (b) `LastModTimeInSecs` and (c) timeline count. None of these proves
  identical content. A file hash changes on every autosave and is not used.
- Decision 2: the bay cannot reach a tailnet-only API. This needs an owner
  decision before #376 and #379 start.
- Decision 7: the scanner is projects-only until #579 says otherwise.

## Still open

1. Whether the project server's `SM_Project_id` equals the bay's UUID for the
   same project (read-only `SELECT` on the server, owner approval needed).
2. Resolve versions in use on other bays.
3. How bays reach the API (decision 2).
