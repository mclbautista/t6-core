"""Drive reads: the inventory, one Drive, serial lookups, shipments and the import exception queue.

Every read goes through the DriveStore interface and gives the typed output of its verb. `list_drives` filters the
way the Drive web app's inventory screen does, and its filters combine with AND.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from t6.drives import Assignment, CatalogSnapshot, CustodyEvent, CustodyKind, Drive, DriveStatus, DriveStore, Ownership, Party, Ref, normalize_serial
from t6.verbs import (DriveCatalogAtOutput, DriveCatalogChange, DriveCatalogDiffOutput, DriveCatalogEntryItem, DriveCatalogSnapshotItem,
                      DriveCatalogSnapshotsOutput, DriveAssignmentItem, DriveEventItem, DriveExceptionItem, DriveExceptionsOutput, DriveFindOutput, DriveGetOutput, DriveItem,
                      DriveListItem, DriveListOutput, DriveMatch, DriveOverdueItem, DriveOverdueOutput, DriveParty, DriveRef, DriveRelease, DriveReloadReport, DriveReloadReportOutput, DriveShipmentItem, DriveShipmentOutput,
                      DriveShipmentsOutput, DriveSourceItem)


def _day(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _ref(ref: Ref | None) -> DriveRef:
    return DriveRef(page_id=ref.page_id if ref else None, text=ref.text if ref else None)


def _party(party: Party | None) -> DriveParty | None:
    return DriveParty(kind=party.kind.value, ref=_ref(party.ref)) if party else None


def _status_value(status: DriveStatus | None) -> str:
    return status.value if status is not None else 'unknown'


def _drive_fields(drive: Drive, status: DriveStatus | None) -> dict[str, Any]:
    return {
        'id': drive.id, 'label': drive.label, 'description': drive.description, 'serial': drive.serial,
        'suspected_serial': drive.suspected_serial, 'needs_review': drive.needs_review, 'review_note': drive.review_note,
        'brand': drive.brand, 'model': drive.model, 'capacity_tb': drive.capacity_tb, 'format': drive.format,
        'form_factor': drive.form_factor.value if drive.form_factor else None, 'ownership': drive.ownership.value,
        'owner_studio_id': drive.owner_studio_id, 'purchase_date': _day(drive.purchase_date), 'warranty': drive.warranty,
        'remarks': drive.remarks, 'office_location': drive.office_location, 'qr_code': drive.qr_code,
        'version': drive.version, 'status': _status_value(status), 'place': drive.office_location,
    }


def _item(store: DriveStore, drive: Drive) -> DriveItem:
    return DriveItem(**_drive_fields(drive, store.status(drive.id)), holder=_holder(store, drive.id))


def _holder(store: DriveStore, drive_id: str) -> str | None:
    events = store.list_events(drive_id)
    last = events[-1] if events else None
    if last and last.kind is CustodyKind.RELEASED and last.party:
        return last.party.ref.text or last.party.ref.page_id
    return None


def _event(event: CustodyEvent) -> DriveEventItem:
    return DriveEventItem(kind=event.kind.value, on=_day(event.on), party=_party(event.party), project=_ref(event.project),
                          logged_by=_ref(event.logged_by), reason=event.reason, shipment_id=event.shipment_id)


def _assignment(number: int, assignment: Assignment) -> DriveAssignmentItem:
    return DriveAssignmentItem(number=number, label=assignment.label, project=_ref(assignment.project) if assignment.project else None,
                               role=assignment.role.value if assignment.role else None, start=_day(assignment.start), end=_day(assignment.end),
                               remarks=assignment.remarks, return_date=_day(assignment.return_date), holder=assignment.holder, open=assignment.is_open)


def _slice(items: list[Drive], offset: int, limit: int | None) -> list[Drive]:
    return items[offset : offset + limit] if limit is not None else items[offset:]


def list_drives(store: DriveStore, *, q: str | None = None, serial: str | None = None, needs_review: bool | None = None,
                project: str | None = None, status: str | None = None,
                office_location: str | None = None, limit: int | None = None, offset: int = 0,
                project_codes: dict[str, str] | None = None) -> DriveListOutput:
    serial, project = (serial or '').strip(), (project or '').strip()
    place = (office_location or '').strip().casefold()
    if serial:
        held = store.find_by_serial(serial)
        drives: tuple[Drive, ...] = (held,) if held else ()
    else:
        drives = store.list_drives()
    needle = (q or '').strip().casefold()
    matching_projects = {pid for pid, code in (project_codes or {}).items() if needle in code.casefold()} if needle else set()
    project_matches = {d.id for d in drives if matching_projects and any(
        a.project and a.project.page_id in matching_projects for a in store.list_assignments(d.id))}
    # A registered Project ID, or the text of a text-only Project.
    out = {drive.id for ref in (Ref(page_id=project), Ref(text=project)) for drive, _ in store.open_assignments(ref)} if project else None
    kept = [d for d in drives
            if (not needle or d.id in project_matches or any(needle in (value or '').casefold() for value in (d.id, d.serial, d.suspected_serial, d.label, d.description, d.qr_code, d.office_location)))
            and (not place or place in (d.office_location or '').casefold())
            and (needs_review is None or d.needs_review == needs_review) and (out is None or d.id in out)]
    if status:
        status_ids: list[str] | None = None if not serial and len(kept) == len(drives) else [d.id for d in kept]
        status_by_id = store.statuses(status_ids) if kept else {}
        matched = [d for d in kept if _status_value(status_by_id.get(d.id)) == status]
        total = len(matched)
        page = _slice(matched, offset, limit)
    else:
        total = len(kept)
        page = _slice(kept, offset, limit)
        page_ids: list[str] | None = None if not serial and len(page) == len(drives) else [d.id for d in page]
        status_by_id = store.statuses(page_ids) if page else {}
    assign_ids: list[str] | None = None if not serial and len(page) == len(drives) else [d.id for d in page]
    open_by_drive = store.open_assignments_by_drive(assign_ids) if page else {}
    return DriveListOutput(
        drives=[
            DriveListItem(
                **_drive_fields(d, status_by_id.get(d.id)),
                holder=_holder(store, d.id),
                open_assignments=[_assignment(n, a) for n, a in open_by_drive.get(d.id, ())],
            )
            for d in page
        ],
        total=total,
    )


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


def project_counts(store: DriveStore, latest: dict[Ref, str] | None = None) -> list[dict[str, Any]]:
    projects: dict[Ref, set[str]] = {}
    statuses = store.statuses()
    for drive in store.list_drives():
        for assignment in store.list_assignments(drive.id):
            if assignment.is_open and assignment.project is not None:
                projects.setdefault(assignment.project, set()).add(drive.id)
    return [{'project': _ref(project).model_dump(), 'total': len(ids),
             'in_house': sum(statuses.get(did) is DriveStatus.IN_CUSTODY for did in ids),
             'released': sum(statuses.get(did) is DriveStatus.RELEASED for did in ids),
             'latest_movement': (latest or {}).get(project)}
            for project, ids in projects.items()]


def reload_report(store: DriveStore, stored: tuple[float, dict[str, Any]] | None) -> DriveReloadReportOutput:
    if stored is None:
        return DriveReloadReportOutput(report=None)
    return DriveReloadReportOutput(report=DriveReloadReport(
        **stored[1], at=datetime.fromtimestamp(stored[0], timezone.utc).isoformat(), exception_rows=exceptions(store).exceptions))


def overdue(store: DriveStore, today: date) -> DriveOverdueOutput:
    rows = []
    for drive in store.list_drives():
        if drive.ownership is not Ownership.T6:
            continue
        events = store.list_events(drive.id)
        if not events:
            continue
        last = events[-1]
        if last.kind is not CustodyKind.RELEASED or last.on is None or (today - last.on).days <= 14:
            continue
        facts = store.shipment_facts(last.shipment_id) if last.shipment_id else None
        person = facts[0] if facts and facts[0] else (last.party.ref.text or last.party.ref.page_id) if last.party else None
        rows.append(DriveOverdueItem(drive_id=drive.id, label=drive.label, serial=drive.serial, person=person,
                                    released_on=last.on.isoformat(), days_out=(today - last.on).days))
    rows.sort(key=lambda row: (row.released_on, row.label or '', row.drive_id))
    return DriveOverdueOutput(overdue=rows)
