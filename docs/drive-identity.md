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
trim and uppercase. It does not remove internal whitespace or other punctuation.
Both accepted and suspected values use it. A supplied string that normalizes to
blank raises `ValueError`; use `None` to remove a value. The importer separately
preserves annotated cells as raw notes and only retains a candidate as suspected.

All store operations take exact, case-sensitive permanent IDs. `get_drive(id)`
returns `None` for unknown IDs; history/mutation operations raise `UnknownDrive`
with `drive_id`. `find_by_serial(serial)` is the explicit normalized accepted-only
lookup, returning `None` when absent. It never searches suspected values, labels,
descriptions or former serials. `add_drive` rejects duplicate IDs with
`ValueError`, and accepted-serial collisions with `DuplicateSerial` (`serial`).

`set_serial(id, value)` adds, corrects or removes an accepted value.
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
A monthly movement with exactly one explicit `S/N` can name an accepted Drive or
create a new one; absent/multiple candidates are individual traceable review
items containing the row's evidence, never associated by label. Duplicate
`REMARKS` columns are joined in order. `SPACE` uses the existing capacity parser;
invalid/conflicting capacity aliases are reported, not invented. `T6 IN` records
receipt: date if recorded, otherwise `None`.

Review items contain absolute workbook path, sheet and row, and can include raw
private facts. Keep actual workbooks/reports local; only synthetic cases belong
in tests, documentation and public artifacts.

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
Store implementers must add serial/review mutation APIs and atomic import receipts.

If an external adapter already has persisted serial/provisional keys, its owner
must explicitly build and retain a one-to-one old-key-to-permanent-ID mapping,
move all Assignment/event/shipment references through it without reordering or
rewriting events, and validate accepted-serial uniqueness before activation.
That conditional adapter migration is not evidence that such a deployment exists,
and is not executed here. Real custodian decisions and cutover remain #55.

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
