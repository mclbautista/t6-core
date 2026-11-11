# Permanent Drive identity and compatibility

Issue #89 replaces the serial/provisional-key contract from #46, #47 and #51.
Project identity ADR 0001 is unchanged. No real workbook import or cutover is
performed; #55 remains human review and operational work.

## Public contract

`Drive` is immutable, keyword-only and equal/hashable by `id` alone. Construction
mints `t6d_` followed by 32 lowercase UUID4 hex digits. A caller can supply a valid
ID to rehydrate an existing record; adapters must retain it, never remint/reuse
it. `Drive.key` aliases `id` from creation. Two independently constructed Drives
with the same serial are different entities, though the store rejects recording
both with that accepted serial. Compare full facts explicitly (for example,
`dataclasses.asdict`) when testing atomicity, not entity equality.

A nonblank accepted/suspected serial, label, description, brand, model or physical
remarks supplies identifying detail. A generated ID, review flag/note, capacity
or owner alone does not. Blank text facts become `None`. An Assignment label may
be absent when the physical Drive has other identifying detail.

Serial normalization is unchanged: remove parenthesized/square-bracketed notes,
trim, collapse runs of internal whitespace to one space and uppercase. It does
not delete the remaining internal spaces or other punctuation.
Both accepted and suspected values use it. A supplied string that normalizes to
blank raises `ValueError`; use `None` to remove a value. The importer separately
preserves annotated cells in traceable review items/notes and only retains the
complete normalized candidate as suspected. Serial-cell text is not copied into
physical remarks and cannot supply a second identifying fact.

All store operations take exact, case-sensitive permanent IDs. `get_drive(id)`
returns `None` for unknown IDs; history/mutation operations raise `UnknownDrive`
with `drive_id`. `find_by_serial(serial)` is the explicit normalized accepted-only
lookup, returning `None` when absent. It never searches suspected values, labels,
descriptions or former serials. `add_drive` rejects duplicate IDs with
`ValueError`, and accepted-serial collisions with `DuplicateSerial` (`serial`).

`set_serial(id, value)` adds, corrects or removes an accepted value.
The minimum-identifying-detail rule applies to edits too: removing a sole serial
raises `ValueError` atomically. First explicitly record another meaningful detail
(for example a genuinely suspected candidate with `set_suspected_serial`). History
and Assignment labels do not supply physical identifying facts. Removal never
fabricates a label or automatically retains the old accepted value as suspected.
`set_suspected_serial(id, value)` changes only the uncertain candidate.
`promote_suspected_serial(id)` explicitly accepts and clears the candidate;
it raises `ValueError` if absent. All accepted-serial writes check uniqueness
before mutation and fail with `DuplicateSerial` without changing either Drive,
notes, history, status or shipment membership. They never demote or merge.
`set_review(id, needs_review=..., note=...)` replaces general review state;
serial/review edits do not append or rewrite custody events.

```python
from t6.drives import Drive
from t6.drives.testing import InMemoryDriveStore

store = InMemoryDriveStore()
drive = store.add_drive(Drive(description="invented blue triangular enclosure",
                              suspected_serial="SYNTH-EXAMPLE", needs_review=True))
assert store.find_by_serial("SYNTH-EXAMPLE") is None
accepted = store.promote_suspected_serial(drive.id)
assert accepted.id == drive.id
store.set_serial(drive.id, "SYNTH-CORRECTED")
store.set_serial(drive.id, None)
assert store.get_drive(drive.id) is not None
```

Assignments, receive, release, return and retirement remain available regardless
of serial/review state. Existing event validation (such as a retirement reason)
still applies. A manifest carries `drive_id`, accepted `serial`, separately named
`suspected_serial`, available label/description/capacity and review flag/note.
Physical facts are current, while the release event and historical Assignment
selection are unchanged. Repeated release rows yield repeated manifest lines.

## Import receipts

Both workbook entrypoints use `DriveStore.import_row(row_key, drive, assignment,
events)` and `imported_drive(row_key)`. A persistent adapter must implement
`import_row` as one transaction: if a receipt exists do nothing; otherwise match
only an accepted serial or add the new ID, retain existing physical facts,
combine incoming review notes, add an Assignment unless an equal one exists,
append all supplied events in order and persist the row-key-to-ID receipt.
Serialize this transaction against concurrent serial changes and other imports.
The in-memory fake is single-process/single-threaded, not a durable store.

Keys are SHA-256 of JSON `[absolute workbook path, sheet, row number]`, not
physical identity or per-field provenance. Distinct row numbers retain legitimate
identical movements. Unchanged reruns at the same path keep IDs, Assignments,
events and shipment memberships unchanged and return review entries once each.
`ImportResult.imported` counts readable rows, including previously imported rows,
not newly created Drives. Reports are rebuilt per invocation, not accumulated.

This is an unchanged-input rerun contract, not workbook synchronization. Do not
move/rename files, insert rows or edit previously imported rows and expect a
receipt to track those changes. Such reconciliation needs explicit human review;
no similarity matching or merge is provided. Receipts remain valid after serial
correction/removal; reruns do not recreate a Drive under its former serial.

Missing/noted/shared inventory serials create distinct review-flagged Drives when
meaningful facts exist. Missing labels do not discard meaningful records. Clean
accepted serials can match across workbooks, but additional/conflicting physical
observations are preserved in an `additional drive facts` review item rather than
overwriting an existing record. Reused labels and descriptions never match.
A monthly movement with exactly one complete, clean explicit `S/N` token can name
an accepted Drive or create a new one. Repeated markers are ambiguous even when
the candidates are identical. The entire suffix after the marker must be one
clean token; trailing notes, internal whitespace and unsupported punctuation go
to review rather than matching a prefix. Remarks preceding the marker can supply
physical notes; the full movement remarks remain in the custody event reason.
A candidate recorded as suspected in the store or
noted/shared in this workbook's inventory blocks automatic association, even when
an inventory row is unreadable. `has_suspected_serial` is a normalized review guard,
not an identity lookup. Absent/multiple/unsafe candidates are individual traceable review
items containing the row's evidence, never associated by label. Duplicate
`REMARKS` columns are joined in order. `SPACE` uses the existing capacity parser;
invalid/conflicting capacity aliases are reported, not invented. `T6 IN` records
receipt: date if recorded, otherwise `None`.
An unreadable or conflicting capacity or date does not drop the row: the Drive
and its other facts are imported, the offending fact is left blank (`capacity_tb`
or the event's `on` is `None`; a recorded but unreadable date still records its
event, undated), and the Drive is flagged for review. The local `unreadable`
review item preserves all the row's cells alongside the reason. A row with no
identifying detail beyond the unreadable cell creates no Drive, and a movement
whose `IN/OUT` cannot be read appends no event; both stay `unreadable` review items.

Review items contain absolute workbook path, sheet and row, and can include raw
private facts. Keep actual workbooks/reports local; only synthetic cases belong
in tests, documentation and public artifacts.

## Operational acceptance and cutover (#55)

The owner has waived the one-time drive-custodian session and accepts dirty
production data. Usable records may be imported without mandatory workbook
cleanup or an individually human-approved decision for every report item. Keep
missing/suspected serials, unresolved Projects as text, and review flags/notes
explicit. Existing identity, accepted-serial uniqueness and custody validation
still apply; never guess identity or automatically merge records.

Accepting dirty data does not permit silent data loss. Before cutover, preserve
unsafe, unreadable or unattached source rows, their evidence and source
traceability in a durable private exception queue for later review. Reports are
rebuilt per invocation and the fake is not durable: neither alone satisfies this
operational preservation gate. Real workbooks, reports and exception evidence,
including private workbook names, serials and client/Project data, stay outside
the repository, fixtures, tests and GitHub/public artifacts.

The workbooks are frozen only at the verified real import/cutover into the web
app's durable store, after verifying preservation of imported records, history
and private exceptions (web-app spec). A dry run, synthetic tests, report
acceptance or this documentation change neither performs nor authorizes that
operation. #55 remains open for those operational gates, with #53 and #54 as its
listed blockers; the custodian session and per-item approval are not gates.

## Compatibility decision and consumers

Repository discovery at baseline `71e9724` found only the DriveStore protocol,
the in-memory fake, importer, manifest helper, CLI dry-run and tests (including
Project registry alias integration). There is no durable Drive store or Drive
schema in this repository. The CLI uses the updated fake and serializes the
report as before, now also including `workbook`. Tests and public examples move
to permanent IDs. No persisted deployment is assumed and no data migration runs.

This is a documented breaking API change: remove `provisional_id`; replace
`get_drive(serial)` with `find_by_serial(serial)` followed by `.id` for subsequent
operations; pass IDs to custody, shipment and Assignment methods; use
`UnknownDrive.drive_id` instead of `.serial`; consume the new manifest fields
and optional Assignment label. Do not introduce a fallback that treats serials
as IDs. `set_serial` now works for any Drive, not only provisional promotion.
Store implementers must add serial/review mutation APIs, `has_suspected_serial`
and atomic import receipts.

If an external adapter already has persisted serial/provisional keys, its owner
must explicitly build and retain a one-to-one old-key-to-permanent-ID mapping,
move all Assignment/event/shipment references through it without reordering or
rewriting events, and validate accepted-serial uniqueness before activation.
That conditional adapter migration is not evidence that such a deployment exists,
and is not executed here. Operational acceptance and cutover remain #55 under
the policy above; unresolved review items need not all be individually approved.

## Synthetic acceptance evidence

- `tests/test_drive_identity.py`: creation/detail validation, ID-only equality,
  serial lookup/edit/promotion, complete collision snapshots and workflows/manifests.
- `tests/test_drive_import_identity.py`: both import paths, uncertainty and notes,
  duplicate remarks, SPACE validation, dated/undated T6 IN, cross-workbook facts,
  row-level missing-serial review and repeated movement reruns/shipments.
- `tests/test_drives.py`, `tests/test_drives_importer.py`: existing domain/import
  regression coverage updated to explicit accepted-serial lookup and permanent IDs.
- `tests/test_registry_migration.py`: Project alias lookup remains independent of
  permanent Drive identity. CLI dry-run coverage remains in importer tests.
