# 8. The bay scanner boundary

Date: 2026-11-22. Status: accepted (owner, 2026-11-22). Source: #18, #375. Facts: #374, see
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

1. **Code: a standalone script, with the format defined in t6-core.** The
   scan is one Python file that uses only the standard library. It runs on the
   bay under the Python that Munki already installs on every managed Mac
   (`/usr/local/munki/munki-python`), so bays need no t6 install and no pinned
   release. It prints one report as JSON on stdout and does nothing else: no
   network, no credential, no state left behind. The report format is a
   versioned model in t6-core. A contract test runs the script against a
   fixture and validates its output with that model. The script lives in
   t6-core next to the model, so the two change in the same PR.
   *Why:* every bay would otherwise need t6 and its dependencies kept at one
   release, which is ADR 0002 rule 7's cost paid on every bay for a job that
   only reads files. Munki's Python is already on every bay.

2. **Report path: the Mac mini pulls, then posts to the t6 API.** The owner
   decided (2026-11-22) that the API stays tailnet-only (ADR 0006 unchanged),
   and #374 found bays are not on the tailnet. So bays never talk to the API.
   The Mac mini, which is on the tailnet and already reaches every bay over
   SSH with the fleet key, runs a committed read-only Ansible playbook in
   t6-munki-repo. For each bay it copies the script over, runs it, collects
   the JSON, removes the script, and posts each report to the t6 API write
   verb `bay scan report`. The playbook is read-only on the bay in the sense
   of the fleet rules (munki #32): agents may run it without a per-run
   approval. The poster holds a new credential, `bay-scanner`: a service
   token whose only permission, `bay_scan`, allows that one verb. It lives on
   the Mac mini only (1Password, like the other fleet secrets); no bay holds
   a secret. ADR 0003 gains a row for it.
   *Why:* this keeps the API tailnet-only, needs no new listener, and puts no
   token on a bay or in the Munki repo, which is served over plain HTTP.
   *Accepted risk:* the Mac mini is a single point: if it is off, no bay
   reports that night, and the drift report shows the reports as stale.

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

5. **Cadence and staleness.** launchd on the Mac mini runs the playbook
   nightly at 02:00 (Manila); the script runs on each bay with low priority
   I/O. A bay that is asleep, off or unreachable is skipped for that night and
   logged; nothing is retried until the next run. The
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
- Bays need nothing new: no package, no launchd job, no token. munki #20
  becomes the read-only playbook and the Mac mini launchd job, not a Munki
  recipe.
- The drift report is only as fresh as the last night each bay was awake and
  the Mac mini was on.
- Nothing in this path writes to Resolve, Notion or a bay.
