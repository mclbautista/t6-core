# Bay stamp observation (design for #381)

Status: **proposed**, awaiting the owner's approval of the open items. No code is written until it is approved and the
prerequisites below are met. Parent: #18. Input to #274.

The bay scanner stays projects-only ([ADR 0008](adr/0008-bay-scanner-boundary.md), decision 7). This is the
Resolve-running path that decision calls for: it reads timeline stamps from a Resolve that is running on a bay.

## What it reports

For each bay, one **observation** per project copy it could inspect, and one explicit result for everything it could not.

Per inspected project copy:

- the Bay (from the machine serial, ADR 0008 decision 4) and the project copy: Resolve project name, and the project
  unique ID if the scripting API returns one (a prerequisite to confirm);
- for each timeline: name, Resolve unique ID (a hint only) and the stamp read from the first-frame marker custom data,
  or the explicit result `no_stamp`;
- duplicate stamps are kept as read (two timelines, one stamp), and a renamed timeline keeps its stamp;
- `observed_at`.

Per bay, a **coverage** record that says why anything was not inspected. These are never shown as `no_stamp`:

| Result | Meaning |
|---|---|
| `observed` | The project copy was read; each timeline has a stamp or `no_stamp`. |
| `not_inspected: resolve_not_running` | No Resolve process on the bay. Nothing was read. |
| `not_inspected: no_project_open` | Resolve is running with no project open. |
| `not_inspected: scripting_unavailable` | Resolve is running but the scripting API does not answer (free version, scripting set to None, or a failed import). |
| `not_inspected: unreachable` | The bay did not answer over SSH (asleep or off). |
| `not_inspected: other_copies` | Project copies that exist on disk (the scanner knows them) but were not loaded. |
| `failed` | A read raised an error; the reason is kept. |
| `stale` | The latest observation is older than 3 days (same threshold as ADR 0008 decision 5). |

No absence of evidence is a clean scan. A Bay with no observation is "no observation", never "no stamps".

## Access: only the project that is already open

A Resolve-running path can read a project only through the scripting API, and loading a project makes it the editor's
current project, locks it and closes the one they had open. That disturbs an editor at work. The design therefore:

1. **Reads only the currently open project** (`GetCurrentProject`, then each timeline). It never loads, switches, opens or
   saves a project, and never changes the current database.
2. Treats every other project copy on the bay as `not_inspected: other_copies`. Coverage is partial by design, and the report
   says so. A bay's full set of copies stays the scanner's job (projects-only).
3. Runs only when Resolve is already running. This is the opposite of the scanner, which skips when Resolve is open, so the
   two paths never read the same moment.

If the owner later wants to inspect copies that are not open, that is a separate decision with an approved window and a named
bay, not part of this design.

## Safety

- Read-only calls only: reading names, unique IDs, markers and custom data. No `AddMarker`, `Update*`, `Delete*`, `Save*`,
  `Load*`, `Import*`, `Export*`, `Archive*` or render call. A test over a fake scripting interface fails if any such call is
  made. (`ArchiveProject` with two arguments crashed Resolve on the 202 Mac Pro; it is never called.)
- Does not start Resolve, does not change scripting preferences, writes no stamp and does no fix (stamping stays the Resolve
  worker's job under ADR 0002 rule 10).
- One scripting client at a time: the script checks that nothing else holds the connection and gives up with
  `failed: scripting_busy` rather than waiting.

## Delivery

Same shape as the bay scan, and it follows the rules in t6-munki-repo ADR 0014:

1. A committed, read-only script in t6-core, standard library plus the Resolve scripting module, with a versioned report model
   and a contract test, like `bay_scanner.py`.
2. A committed read-only Ansible playbook in t6-munki-repo runs it on each bay over SSH from the Mac mini (bays are not on the
   tailnet). It copies the script to a private temp folder, runs it, saves the JSON on the Mac mini and removes the folder. A
   bay that is asleep or unreachable is logged as `not_inspected: unreachable`.
3. The Mac mini posts each report to the tailnet-only t6 API as the `bay-scanner` service, through a new write verb
   (working name `bay stamp report`) with its own permission. ADR 0003 gains the permission, not a new credential.
4. The API keeps the latest observation per Bay and project copy, with `observed_at`.

Read verb: `bay stamps <project>` lists the observations for a project, with coverage. The name `bay scan timelines` is not
used, because the source is a running Resolve and not the scanner. The verb must appear in `t6 commands --json`.

## Prerequisites (to settle before code)

1. **Resolve Studio and scripting on the bays.** The scripting API needs Resolve Studio with external scripting set to
   Local. Check each bay's edition and setting (read-only, with the owner's yes per bay). A bay without it is
   `scripting_unavailable` and stays that way until the owner decides.
2. **Which Python imports `fusionscript` on a bay.** Munki's Python may not load Resolve's scripting library. If it does not,
   the script needs another interpreter on the bay, which is an install and needs the owner's approval. Check on one bay first.
3. **A project unique ID.** Confirm whether `Project.GetUniqueId()` exists in the bays' Resolve version. If not, the project
   copy is identified by Bay plus project name plus the scanner's project UUID, matched by the API, and the report says it is
   matched by name.
4. **A woken or inactive Resolve answers `None`.** On the 175 Mac mini (Resolve 20.0.0.49), right after the Mac woke from
   sleep, reads worked but `GetCurrentPage`, `GetCurrentDatabase` and `AddMarker` returned `None`. About 10 minutes later the
   same calls worked (#579). The script must treat `None` from a call that should return a value as `failed: scripting_not_ready`,
   never as an empty result or `no_stamp`. In Resolve 20, `SaveProject` is on the ProjectManager, not the project.
5. **Approval.** ADR 0008 gains an amendment for this path, and the playbook runs under ADR 0014.

## Out of scope

- Reading stamps from a closed disk database (not shown to work; revisit only with an approved stamped control).
- Writing, fixing or re-stamping (the Resolve worker, #577).
- Loading other projects on a bay.

## Acceptance criteria (from #381), and where they land

- Design recorded and prerequisites satisfied before code: this document, then the prerequisites above.
- Fixtures or a fake scripting interface for stamped, unstamped and duplicate-stamp timelines, with Bay and copy intact.
- Report and storage round-trip a stamp unchanged; a renamed timeline keeps it.
- Unavailable, skipped, failed and stale are distinct from `no_stamp`.
- No stamp written, no automatic fix.
- The read verb is in `t6 commands --json`.
