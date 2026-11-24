"""Dated last releases more than fourteen Manila days old."""
from collections.abc import Iterable
from datetime import date

from t6.drives import CustodyKind, Drive, DriveStore
from t6.verbs import DriveOverdueItem


def overdue_drives(store: DriveStore, drives: Iterable[Drive], today: date) -> list[DriveOverdueItem]:
    rows = []
    for drive in drives:
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
    return rows
