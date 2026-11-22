"""Drive reads: the inventory, one Drive, serial lookups, shipments and the import exception queue.

Every read goes through the DriveStore interface and gives the typed output of its verb. `list_drives` filters the
way the Drive web app's inventory screen does, and its filters combine with AND.
"""
from __future__ import annotations

from datetime import date

from t6.drives import Assignment, CatalogSnapshot, CustodyEvent, Drive, DriveStore, Party, Ref, normalize_serial
from t6.verbs import (DriveCatalogAtOutput, DriveCatalogChange, DriveCatalogDiffOutput, DriveCatalogEntryItem, DriveCatalogSnapshotItem,
                      DriveCatalogSnapshotsOutput, DriveAssignmentItem, DriveEventItem, DriveExceptionItem, DriveExceptionsOutput, DriveFindOutput, DriveGetOutput, DriveItem,
                      DriveListItem, DriveListOutput, DriveMatch, DriveParty, DriveRef, DriveRelease, DriveShipmentItem, DriveShipmentOutput,
                      DriveShipmentsOutput, DriveSourceItem)


def _day(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _ref(ref: Ref) -> DriveRef:
    return DriveRef(page_id=ref.page_id, text=ref.text)


def _party(party: Party | None) -> DriveParty | None:
    return DriveParty(kind=party.kind.value, ref=_ref(party.ref)) if party else None


def _item(store: DriveStore, drive: Drive) -> DriveItem:
    status = store.status(drive.id)
    return DriveItem(id=drive.id, label=drive.label, description=drive.description, serial=drive.serial, suspected_serial=drive.suspected_serial,
                     needs_review=drive.needs_review, review_note=drive.review_note, brand=drive.brand, model=drive.model, capacity_tb=drive.capacity_tb,
                     format=drive.format, form_factor=drive.form_factor.value if drive.form_factor else None, ownership=drive.ownership.value,
                     owner_studio_id=drive.owner_studio_id, purchase_date=_day(drive.purchase_date), warranty=drive.warranty, remarks=drive.remarks,
                     office_location=drive.office_location, qr_code=drive.qr_code, version=drive.version, status=status.value if status else 'unknown')


def _event(event: CustodyEvent) -> DriveEventItem:
    return DriveEventItem(kind=event.kind.value, on=_day(event.on), party=_party(event.party), project=_ref(event.project),
                          logged_by=_ref(event.logged_by), reason=event.reason, shipment_id=event.shipment_id)


def _assignment(number: int, assignment: Assignment) -> DriveAssignmentItem:
    return DriveAssignmentItem(number=number, label=assignment.label, project=_ref(assignment.project) if assignment.project else None,
                               role=assignment.role.value if assignment.role else None, start=_day(assignment.start), end=_day(assignment.end),
                               remarks=assignment.remarks, return_date=_day(assignment.return_date), holder=assignment.holder, open=assignment.is_open)


def list_drives(store: DriveStore, *, q: str | None = None, serial: str | None = None, needs_review: bool | None = None,
                project: str | None = None, status: str | None = None,
                office_location: str | None = None) -> DriveListOutput:
    serial, project = (serial or '').strip(), (project or '').strip()
    place = (office_location or '').strip().casefold()
    drives: tuple[Drive, ...] = store.list_drives()
    if serial:
        held = store.find_by_serial(serial)
        drives = (held,) if held else ()
    needle = (q or '').strip().casefold()
    out = {drive.id for drive, _ in store.open_assignments(Ref(page_id=project))} if project else None
    kept = [d for d in drives
            if (not needle or any(needle in (value or '').casefold() for value in (d.id, d.serial, d.suspected_serial, d.label, d.description, d.qr_code, d.office_location)))
            and (not place or place in (d.office_location or '').casefold())
            and (needs_review is None or d.needs_review == needs_review) and (out is None or d.id in out)]
    items = [(_item(store, d), d) for d in kept]
    return DriveListOutput(drives=[DriveListItem(**item.model_dump(), open_assignments=[_assignment(n, a) for n, a in enumerate(store.list_assignments(d.id), 1) if a.is_open])
                                   for item, d in items if not status or item.status == status])


def get(store: DriveStore, drive_id: str) -> DriveGetOutput | None:
    drive = store.get_drive(drive_id)
    if drive is None:
        return None
    return DriveGetOutput(drive=_item(store, drive), events=[_event(e) for e in store.list_events(drive_id)],
                          assignments=[_assignment(number, a) for number, a in enumerate(store.list_assignments(drive_id), 1)],
                          sources=[DriveSourceItem(workbook=s.workbook, sheet=s.sheet, row=s.row) for s in store.sources(drive_id)],
                          deliveries=list(store.drive_deliveries(drive_id)))


def find(store: DriveStore, serial: str | None = None, qr_code: str | None = None) -> DriveFindOutput:
    if qr_code and qr_code.strip():
        try:
            held = store.find_by_qr_code(qr_code)
        except ValueError:
            held = None
        return DriveFindOutput(qr_code=held.qr_code if held else qr_code.strip().upper(),
                               matches=[DriveMatch(match='qr_code', **_item(store, held).model_dump())] if held else [])
    normalized = normalize_serial(serial or '')
    held = store.find_by_serial(serial or '')
    matches = [DriveMatch(match='accepted', **_item(store, held).model_dump())] if held else []
    matches += [DriveMatch(match='suspected', **_item(store, d).model_dump()) for d in store.list_drives() if normalized and d.suspected_serial == normalized]
    return DriveFindOutput(serial=normalized, matches=matches)


def _snapshot(snapshot: CatalogSnapshot) -> DriveCatalogSnapshotItem:
    return DriveCatalogSnapshotItem(id=snapshot.id, captured_on=snapshot.captured_on.isoformat(), parent_snapshot_id=snapshot.parent_snapshot_id,
                                    root_path=snapshot.root_path, entry_count=snapshot.entry_count, total_bytes=snapshot.total_bytes)


def catalog_snapshots(store: DriveStore, drive_id: str) -> DriveCatalogSnapshotsOutput | None:
    if store.get_drive(drive_id) is None:
        return None
    return DriveCatalogSnapshotsOutput(snapshots=[_snapshot(s) for s in store.catalog_snapshots(drive_id)])


def catalog_diff(store: DriveStore, drive_id: str, snapshot_id: str) -> DriveCatalogDiffOutput | None:
    if store.get_drive(drive_id) is None:
        return None
    changes = store.catalog_diff(drive_id, snapshot_id)
    snapshot = next((s for s in store.catalog_snapshots(drive_id) if s.id == snapshot_id), None)
    if changes is None or snapshot is None:
        return None
    return DriveCatalogDiffOutput(snapshot=_snapshot(snapshot), changes=[
        DriveCatalogChange(path=c.path, kind=c.kind, change=c.change, size_bytes=c.size_bytes, mtime=c.mtime, checksum=c.checksum) for c in changes])


def catalog_at(store: DriveStore, drive_id: str, on: date, prefix: str | None = None) -> DriveCatalogAtOutput | None:
    if store.get_drive(drive_id) is None:
        return None
    found = store.contents_at(drive_id, on)
    if found is None:
        return None
    folder = (prefix or '').strip().strip('/')
    kept = [e for e in found.entries if not folder or e.path == folder or e.path.startswith(folder + '/')]
    return DriveCatalogAtOutput(snapshot=_snapshot(found.snapshot), entries=[
        DriveCatalogEntryItem(path=e.path, kind=e.kind, size_bytes=e.size_bytes, mtime=e.mtime, checksum=e.checksum) for e in kept])


def shipments(store: DriveStore) -> DriveShipmentsOutput:
    found = []
    for shipment_id in store.list_shipments():
        releases = store.shipment_events(shipment_id)
        first = releases[0][1]
        found.append(DriveShipmentItem(shipment_id=shipment_id, on=_day(first.on), party=_party(first.party), project=_ref(first.project),
                                       drive_count=len({drive.id for drive, _ in releases})))
    return DriveShipmentsOutput(shipments=found)


def shipment(store: DriveStore, shipment_id: str) -> DriveShipmentOutput | None:
    releases = store.shipment_events(shipment_id)
    if not releases:
        return None
    kept = store.shipment_facts(shipment_id)
    facts = kept[1] if kept else {}
    return DriveShipmentOutput(shipment_id=shipment_id, recipient=kept[0] if kept else None, facts_at_release=kept is not None,
                               releases=[DriveRelease(drive=_item(store, facts.get(d.id, d)), event=_event(e)) for d, e in releases])


def exceptions(store: DriveStore) -> DriveExceptionsOutput:
    return DriveExceptionsOutput(exceptions=[DriveExceptionItem(workbook=i.workbook, sheet=i.sheet, row=i.row, reason=i.reason, detail=i.detail)
                                             for i in store.exceptions()])
