# 8. The bay scanner boundary

Date: 2026-11-22. Status: proposed. Source: #18, #375. Facts: #374, see
`docs/research/bay-resolve-disk-library.md`.

## Context

#18 asks which bays hold local copies of Resolve projects and whether those
copies match the project server. A nightly job on each bay reads the local
Resolve disk database and reports to t6. Five things need deciding before the
build tickets (#376, #377, #379, #380, #381) start: where the code lives, how a
report reaches t6, the read-only guarantee, how a report is tied to a Bay, and
cadence. Some decisions rest on facts #374 has not confirmed yet. Those are
marked **(confirm in #374)**, and this ADR is amended if a fact turns out
otherwise.

## Decision

1. **Code: a standalone script, with the format defined in t6-core.** The bay
   job is one Python file that uses only the standard library. It runs under
   the Python that Munki already installs on every managed Mac
   (`/usr/local/munki/munki-python`), so bays need no t6 install and no pinned
   release. The report format is a versioned model in t6-core. A contract test
   runs the script against a fixture and validates its output with that model.
   The script lives in t6-core next to the model, so the two change in the same
   PR, and t6-munki-repo packages it (munki #20).
   *Why:* every bay would otherwise need t6 and its dependencies kept at one
   release, which is ADR 0002 rule 7's cost paid on every bay for a job that
   only reads files. Munki's Python is already on every bay.

2. **Report path: the t6 API, one narrow service credential.** The job posts its
   report to a t6 API write verb (`bay scan report`). The job is the holder of a
   new credential, `bay-scanner`: a service token whose only permission,
   `bay_scan`, allows that one verb and nothing else. Munki delivers it as a
   root-owned file with mode 0600, outside the Munki repo's public catalog
   (the exact delivery is part of munki #20). ADR 0003 gains a row for it.
   *Why:* ADR 0002 rule 1 says apps call the API, and a file drop would need a
   share and a collector on the Mac mini. One shared token is chosen over one
   per bay because Munki serves the same files to every client, so per-bay
   secrets would need a delivery path that does not exist yet.
   *Accepted risk:* any bay holding the token could post a report that claims
   another machine's serial. The data is report-only and changes nothing, and
   the API logs the reporting address. Per-bay tokens can replace this later
   without changing the format.
   **Route: tailnet only.** The owner decided (2026-11-22) that the API stays
   tailnet-only and ADR 0006 does not change. A bay posts to the tailnet
   listener that serves `/v1/`. **Open: #374 found the pilot bay is NOT on
   the tailnet** (no Tailscale at all), so it cannot reach the API. The route
   for bays is an owner decision, and this decision is amended when it is
   made. #376 and #379 wait for it.

3. **Read-only guarantee.** The job never opens or starts Resolve. If a Resolve
   process is running, it scans nothing and reports "skipped, Resolve open" (a
   normal night, not an error). It copies the disk database folder to a
   temporary folder and reads only the copy, opening any SQLite file with
   `mode=ro` and `immutable=1`. It deletes the copy when it is done, and it
   never writes inside the Resolve folders. A test proves the fixture folder is
   byte-for-byte unchanged after a scan. This follows ADR 0002 rule 10.
   "Resolve is running" means a process whose executable is inside the
   Resolve app bundle. Confirmed in #374 (Resolve 20.3.1): the library is
   `~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Resolve Project Library/Resolve Projects/Users/<user>/`,
   one plain SQLite `Project.db` per project under nested `Projects/`
   directories, and a closed Resolve leaves no lock, journal, `-wal` or `-shm`
   files. The job reads `Project.db` only and never trusts the
   `ProjectMetadataCache/Metadata.db` cache, which #374 found incomplete and
   stale.

4. **Identity: the serial, mapped by t6.fleet.** The report carries the
   machine's hardware serial number (`IOPlatformSerialNumber`). The API ties it
   to a Bay through the Machine to Bays mapping in `t6.fleet`: serial, then the
   Bays row's Serial Number, then the row's page ID, then the registered Bay
   (`t6b_`). The API never guesses from a name. A serial that maps to no Bay
   row, or to a row with no registered Bay, is refused with rule `no_bay` and
   nothing is stored.
   *Why:* the serial is the one fact both Munki and the Bays row already hold.
   Hostnames change, and names are how `Deco 202` and `Deco 203` got confused
   (#551).

5. **Cadence and staleness.** launchd runs the job nightly at 02:00 (Manila)
   with low priority I/O. If the Mac was asleep, launchd runs it at wake. The
   API keeps the latest report per Bay. The drift report shows a report older
   than 3 days as stale, and a Bay with no report as "no report", never as "no
   copies".

6. **What "matches the server" means.** Per #374, a local copy reports
   `SM_Project.SM_Project_id` (UUID), `SM_Project.ProjectName`, its folder
   path, `SM_Project.LastModTimeInSecs` and its `Sm2Timeline` count. The drift
   report (#380) compares identity by UUID, then modified time and timeline
   count, and calls a copy "same identity, newer/older/equal", never
   "identical": none of these fields proves the same content. A file hash is
   not used because autosave changes it. **Still to confirm:** that the
   project server stores the same UUID for the same project (a read-only
   check on the server). Until then, #380 matches by folder path and name and
   says so.

7. **Timeline stamps: only if readable.** Per `docs/research/resolve-marker-custom-data.md`,
   markers sit in binary blobs on the project server and on bays (#374).
   #579 proved stamps through the scripting API only, so whether a stamp is
   readable inside a disk `Project.db` is unanswered. The scanner is
   therefore projects-only, and #381 reads stamps on a path where Resolve is
   running. Revisit only if a stamped control project shows the stamp in a
   blob. Stamps are written by the Resolve
   worker under the amended ADR 0002 rule 10 (#578), never by this job: the
   scanner only ever reads them.

## Consequences

- t6-core gains a report model, an ingest verb, a `bay_scan` permission and a
  `bay-scanner` service actor. ADR 0003 gains its credential row.
- Bays need nothing new beyond the Munki package (script, launchd plist and
  token file).
- The drift report is only as fresh as the last night each bay was awake.
- Nothing in this path writes to Resolve, Notion or a bay.
