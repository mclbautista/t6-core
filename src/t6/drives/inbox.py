"""The Inbox: Items of kept Captures waiting for the custodian's Approval (#724)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from dataclasses import replace
from typing import Any
from uuid import uuid4

from t6.domain.studio_time import MANILA
from t6.drives import Drive, DuplicateSerial, Ref
from t6.drives.caption import parse_caption
from t6.drives.movements import Movement, _movement, append, custody_event, latest_custody, _append_event
from t6.drives.sqlite import SqliteDriveStore, audit_json
from t6.notion.generated import ProjectsRow
from t6.notion.source import NotionSource
from t6.registry.core import normalize_page_id
from t6.registry.sqlite import SqliteProjectRegistry
from t6.verbs import DriveApproveInput, Done, Outcome, Refused


def inbox(registry: SqliteProjectRegistry) -> Outcome:
    with registry._transaction() as db:
        captures = [(json.loads(row[1]), row[0], row[2]) for row in db.execute('SELECT rowid, data, length(photo) FROM drive_captures')]
    captures.sort(key=lambda found: (datetime.fromisoformat(found[0]['posted_at']), found[1]), reverse=True)
    items: list[dict[str, Any]] = []
    for data, _, photo in captures:
        caption = parse_caption(data['text']) if data['state'] == 'resolved' else None
        if caption is None or caption.problem:  # a caption to fix waits for the poster, not for the custodian
            continue
        items += [{'capture': data['capture'], 'item': number, 'text': item['text'], 'caption': data['text'], 'direction': caption.direction,
                   'recipient': caption.recipient, 'photo_reference': data['capture'] if photo else None, 'guesses': item.get('guesses', []),
                   'chat_id': data['chat_id'], 'topic_id': data.get('topic_id'), 'message_id': data['message_id'], 'posted_at': data['posted_at']}
                  for number, item in enumerate(data['items']) if item['state'] == 'open']
    return Done(result={'items': items})


def approve(registry: SqliteProjectRegistry, inputs: DriveApproveInput, source: NotionSource | None, actor: str) -> Outcome:
    stale_photos: list[str] = []
    with registry._transaction() as db:
        row = db.execute('SELECT data FROM drive_captures WHERE id=?', (inputs.capture,)).fetchone()
        data = json.loads(row[0]) if row else None
        if data is None or data['state'] not in ('resolved', 'undone'):
            return Refused(rule='capture', reason='Capture not found or not yet read')
        if not 0 <= inputs.item < len(data.get('items', [])):
            return Refused(rule='item', reason='Capture has no such Item')
        item = data['items'][inputs.item]
        store = SqliteDriveStore(registry, within=db)
        drive: Drive | None = None
        if inputs.choice == 'drive':
            if inputs.drive is None and inputs.typed is None:
                return Refused(rule='input', reason='Give the Drive, or type its label or serial')
            drive = store.get_drive(inputs.drive) if inputs.drive else _typed(store, inputs.typed or '')
            if drive is None:
                return Refused(rule='drive', reason='Drive not found')
        if item['state'] != 'open':  # a repeat of the settling call returns the same result; anything else is refused
            if (item['state'], inputs.choice) in (('not_a_drive', 'not_a_drive'), ('approved', 'new')) or (
                    item['state'] == 'approved' and drive is not None and item.get('drive') == drive.id):
                return Done(result=_result(inputs, item))
            return Refused(rule='item_settled', reason='Item is already settled')
        if inputs.choice == 'none' and item.get('last_approval') == 'none':
            return Done(result=_result(inputs, item))
        rank = next((n for n, guess in enumerate(item.get('guesses', []), 1) if drive and guess['drive'] == drive.id), None)
        outcome = f'guess{rank}' if inputs.choice == 'drive' and not inputs.typed and rank else 'typed' if inputs.choice == 'drive' else inputs.choice
        if inputs.choice in ('none', 'not_a_drive') and item.get('drive'):
            original = _movement(json.loads(db.execute('SELECT data FROM drive_movements WHERE id=?', (item['movement'],)).fetchone()[0]))
            correction = replace(original, id='t6m_' + uuid4().hex, corrects=original.id, drive=None, to_review=False, remarks='Guess cleared')
            append(db, correction)
            _restore(db, store, item['drive'], correction.id)
            item.update(drive=None, movement=correction.id, guesses=[])
        if inputs.choice == 'not_a_drive':
            item['state'] = 'not_a_drive'
        elif inputs.choice != 'none':
            caption = parse_caption(data['text'])
            if caption.problem:
                return Refused(rule='problem', reason='The caption needs a fix (IN or OUT, and R: for an OUT) before its Items can be approved')
            if drive is None:
                try:
                    drive = store.add_drive(Drive(label=inputs.label or item['text'], serial=inputs.serial))
                except DuplicateSerial as error:
                    return Refused(rule='serial', reason=str(error))
                audit = db.execute('INSERT INTO audit (project_id, action, actor, at, before, after) VALUES (?, ?, ?, ?, ?, ?)',
                                   (drive.id, 'drive_add', actor, registry._clock(), json.dumps(None), json.dumps(audit_json(drive))))
                db.execute('INSERT INTO activity (project_id, subject_id, at, actor, action, summary, audit_id) VALUES (?, ?, ?, ?, ?, ?, ?)',
                           (drive.id, drive.id, registry._clock(), actor, 'drive_add', 'drive add', audit.lastrowid))
            movement = item.get('movement') if item.get('drive') == drive.id else _move(db, store, data, drive, source, corrects=item.get('movement'))
            if item.get('drive') and item['drive'] != drive.id:
                _restore(db, store, item['drive'], movement)
            item.update(state='approved', drive=drive.id, movement=movement)
            from t6.drives.learning import learn
            stale_photos = learn(db, drive.id, item['text'], movement, inputs.capture, actor, registry._clock())
        db.execute('INSERT INTO drive_match_approvals (capture, item, outcome, rank, at, who) VALUES (?, ?, ?, ?, ?, ?)',
                   (inputs.capture, inputs.item, outcome, rank if outcome.startswith('guess') else None, registry._clock(), actor))
        item['last_approval'] = outcome
        db.execute('UPDATE drive_captures SET data=? WHERE id=?', (json.dumps(data), inputs.capture))
        result = Done(result=_result(inputs, item))
    from t6.drives.photos import photo_dir, remove_files
    remove_files(stale_photos, photo_dir())  # after commit: a failed approval keeps the old Reference photo
    return result


def _result(inputs: DriveApproveInput, item: dict[str, Any]) -> dict[str, Any]:
    return {'capture': inputs.capture, 'item': inputs.item, 'state': item['state'], 'drive': item.get('drive'), 'movement': item.get('movement')}


def _typed(store: SqliteDriveStore, text: str) -> Drive | None:
    labelled = [d for d in store.list_drives() if text and d.label == text]
    return labelled[0] if len(labelled) == 1 else store.find_by_serial(text) if text else None


def _move(db: sqlite3.Connection, store: SqliteDriveStore, data: dict[str, Any], drive: Drive, source: NotionSource | None, *, to_review: bool = False, corrects: str | None = None) -> str:
    """Append an Item's Movement and custody event; return the Movement ID."""
    recorder = db.execute('SELECT id FROM persons WHERE telegram_id=?', (data['sender_telegram_id'],)).fetchone()
    logged_by = Ref(page_id=recorder[0]) if recorder else Ref(text=f"telegram:{data['sender_telegram_id']}")
    caption = parse_caption(data['text'])
    direction = caption.direction
    assert direction is not None
    person = caption.recipient if direction == 'OUT' else recorder[0] if recorder else f"telegram:{data['sender_telegram_id']}"
    assignments = [a for a in store.list_assignments(drive.id) if a.end is None]
    project = assignments[0].project if len(assignments) == 1 and assignments[0].project else None
    movement = Movement(id='t6m_' + uuid4().hex, drive=drive.id, label=drive.label, corrects=corrects,
                        on=datetime.fromisoformat(data['posted_at']).astimezone(MANILA).date(), direction=direction, person=person,
                        recorder=recorder[0] if recorder else None, recorder_telegram_id=data['sender_telegram_id'],
                        place=None, project=project, capture=data['capture'], posted_at=data['posted_at'],
                        shipment_id='shipment-' + data['capture'] if direction == 'OUT' else None,
                        to_review=to_review, coordinators=_coordinators(db, project, source) if source is not None else ())
    append(db, movement)
    event = custody_event(movement, logged_by)
    _append_event(db, store, drive.id, event, movement.id)
    if direction == 'OUT':
        store.set_facts(drive.id, {'office_location': None})
    latest = latest_custody(db, drive.id)
    if latest and latest != event:
        _append_event(db, store, drive.id, latest, movement.id)
    return movement.id


def _coordinators(db: sqlite3.Connection, project: Ref | None, source: NotionSource) -> tuple[str, ...]:
    """The Persons in the Project's `T6 Post Coordinator`, read from the Notion mirror; a Contact with no Person stays its page ID.

    Nothing is known without a registered Project with a Notion page or a Notion source: the Movement then has no coordinators.
    """
    if project is None or project.page_id is None:
        return ()
    page = db.execute("SELECT page_id FROM bindings WHERE system='notion' AND project_id=? AND active=1", (project.page_id,)).fetchone()
    if page is None:
        return ()
    try:
        contacts = ProjectsRow.from_page(source.retrieve_page(page[0], freshness='mirror')).t6_post_coordinator
    except Exception:  # a Notion failure never stops a Movement
        return ()
    bound = [db.execute("SELECT project_id FROM bindings WHERE system='notion' AND page_id=?", (normalize_page_id(contact),)).fetchone() for contact in contacts]
    return tuple(dict.fromkeys(found[0] if found else contact for found, contact in zip(bound, contacts)))


def _restore(db: sqlite3.Connection, store: SqliteDriveStore, drive: str, movement: str) -> None:
    """Append the effective custody after a guess was corrected away from this Drive."""
    previous = latest_custody(db, drive)
    if previous is not None:
        _append_event(db, store, drive, previous, movement)
    else:
        from t6.drives import CustodyEvent, CustodyKind
        _append_event(db, store, drive, CustodyEvent(kind=CustodyKind.RECEIVED, on=None, party=None, project=None, logged_by=Ref(text='guess cleared')), movement)
        db.execute('INSERT INTO drive_status_resets VALUES (last_insert_rowid())')
