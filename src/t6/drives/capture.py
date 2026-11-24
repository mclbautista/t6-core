"""Durable recording posts, resolved asynchronously after admission."""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from t6.domain.events import JsonValue
from t6.drives import Ref
from t6.drives.movements import Movement, active_movements, append, custody_event, latest_custody, _append_event
from t6.drives.debug import DebugNotifier
from t6.drives.sqlite import SqliteDriveStore
from t6.llm.drive_photo import read_drive_photo
from t6.llm.drive_transmittal import judge_transmittal
from t6.llm.transport import Image, ImageType, Provider
from t6.worker import WorkerJob

from t6.drives.recording import recording_input
from t6.registry.sqlite import SqliteProjectRegistry
from t6.verbs import Done, DriveCaptureInput, Outcome, Refused
from t6.worker.internal_queue import InternalJobQueue


def recording_chat(environ: Mapping[str, str] | None = None) -> int | None:
    value = (os.environ if environ is None else environ).get('T6_DRIVE_RECORDING_CHAT_ID')
    return int(value) if value else None


def recording_topic(environ: Mapping[str, str] | None = None) -> int | None:
    value = (os.environ if environ is None else environ).get('T6_DRIVE_RECORDING_TOPIC_ID')
    return int(value) if value else None


UNDO_WINDOW_SECONDS = 3600
_DIRECTION = re.compile(r'(in|out)(?=[\s:\-]|$)[\s:\-]*', re.IGNORECASE)
_PLACES = ('TABLE', 'CABINET', 'SHELF', 'BOX')


def _parse_text(text: str) -> tuple[Literal['IN', 'OUT'] | None, str, Literal['TABLE', 'CABINET', 'SHELF', 'BOX'] | None, str]:
    """Read a caption or follow-up answer as (direction, person, place, typed label); direction is None without a leading IN/OUT."""
    command, _, typed = text.strip().replace('|', '\n', 1).partition('\n')
    typed = typed.strip()
    start = _DIRECTION.match(command.strip())
    if start is None:
        return None, '', None, typed or command.strip()
    direction: Literal['IN', 'OUT'] = 'IN' if start[1].upper() == 'IN' else 'OUT'
    tail = command.strip()[start.end():].strip()
    if direction == 'OUT':
        return direction, tail, None, typed
    first, _, rest = tail.partition(' ')
    if first.upper() in _PLACES:
        return direction, '', first.upper(), typed or rest.strip()  # type: ignore[return-value]
    return direction, '', None, typed or tail


def capture_post(registry: SqliteProjectRegistry, inputs: DriveCaptureInput) -> Outcome:
    if inputs.capture:
        queue = InternalJobQueue(registry)
        with registry._transaction() as db:
            row = db.execute('SELECT data FROM drive_captures WHERE id=?', (inputs.capture,)).fetchone()
            if row is None:
                return Refused(rule='capture', reason='Capture not found')
            data = json.loads(row[0])
            if recording_input(data['chat_id'], '', b'', recording_chat_id=recording_chat(),
                               topic_id=data.get('topic_id'), recording_topic_id=recording_topic()) is None:
                return Refused(rule='recording_channel', reason='Only the configured recording channel is accepted')
            if not inputs.drive and not inputs.text.strip():
                return Refused(rule='input', reason='Give a label, serial or Drive')
            if inputs.drive and SqliteDriveStore(registry, within=db).get_drive(inputs.drive) is None:
                return Refused(rule='drive', reason='Drive not found')
            if inputs.edit and inputs.drive:
                return Refused(rule='input', reason='An edit replaces the caption text, not the Drive')
            if inputs.edit:  # replaces the caption only: any follow-up already given and the one-question allowance stay as they are
                if data['state'] not in ('pending', 'exception') or data.get('text') == inputs.text:
                    return Done(result=_result(data))
                data.update(text=inputs.text, revision=data.get('revision', 0) + 1, state='pending', reason=None, differences=[], edited=True)
                db.execute('UPDATE drive_captures SET data=? WHERE id=?', (json.dumps(data), inputs.capture))
                queue.submit('main', 'drive.capture.resolve', f"{inputs.capture}:{data['revision']}",
                             {'capture': inputs.capture, 'revision': data['revision']}, db=db)
                return Done(result=_result(data))
            if data['state'] in ('resolved', 'undone', 'ignored') or data['state'] == 'pending' and (
                    data.get('follow_up_drive') == inputs.drive if inputs.drive else not data.get('follow_up_drive') and data.get('follow_up') == inputs.text.strip()):
                return Done(result=_result(data))
            data.update(follow_up=inputs.text.strip(), follow_up_drive=inputs.drive, revision=data.get('revision', 0) + 1, state='pending', reason=None, answered=True)
            db.execute('UPDATE drive_captures SET data=? WHERE id=?', (json.dumps(data), inputs.capture))
            queue.submit('main', 'drive.capture.resolve', f"{inputs.capture}:{data['revision']}",
                         {'capture': inputs.capture, 'revision': data['revision']}, db=db)
            return Done(result=_result(data))
    if inputs.drive:
        return Refused(rule='input', reason='A Drive is only given for an existing capture')
    if any(value is None for value in (inputs.chat_id, inputs.message_id, inputs.sender_telegram_id, inputs.posted_at)) or not (inputs.photo or inputs.text.strip()):
        return Refused(rule='input', reason='Give chat, message, sender, post time and a photo or text')
    assert inputs.chat_id is not None
    try:
        configured = recording_chat()
        topic = recording_topic()
        photo = base64.b64decode(inputs.photo, validate=True) if inputs.photo else b''  # a text-only post has no photo
    except (ValueError, binascii.Error):
        return Refused(rule='input', reason='Invalid recording channel configuration or base64 photo')
    admitted = recording_input(inputs.chat_id, inputs.text, photo, recording_chat_id=configured,
                               topic_id=inputs.topic_id, recording_topic_id=topic)
    if admitted is None:
        return Refused(rule='recording_channel', reason='Only the configured recording channel is accepted')
    key = f'{inputs.chat_id}:{inputs.message_id}'
    queue = InternalJobQueue(registry)
    with registry._transaction() as db:
        existing = db.execute('SELECT data FROM drive_captures WHERE telegram_key=?', (key,)).fetchone()
        if existing:
            return Done(result=_result(json.loads(existing[0])))
        data = inputs.model_dump(mode='json', exclude={'photo', 'capture'})
        if admitted.photo.startswith(b'\xff\xd8\xff'):
            data['media_type'] = 'image/jpeg'
        data.update(capture='capture_' + uuid4().hex, state='pending', lines=[], differences=[])
        db.execute('INSERT INTO drive_captures VALUES (?, ?, ?, ?)', (data['capture'], key, json.dumps(data), admitted.photo))
        queue.submit('main', 'drive.capture.resolve', data['capture'], {'capture': data['capture']}, db=db)
        return Done(result=_result(data))


def _undo_until(data: dict[str, Any]) -> str | None:
    if data['state'] != 'resolved' or data.get('resolved_at') is None:
        return None
    return datetime.fromtimestamp(data['resolved_at'] + UNDO_WINDOW_SECONDS, UTC).isoformat()


def _result(data: dict[str, Any]) -> dict[str, Any]:
    return {key: data[key] for key in ('capture', 'state', 'lines', 'differences')} | {'photo_reference': None if data['state'] == 'ignored' else data['capture'], 'message': 'ask for the label' if data['state'] == 'exception' else None,
            'reason': data.get('reason') if data['state'] == 'exception' else None,
            'may_ask': data['state'] == 'exception' and not data.get('answered') and data.get('reason') != 'no_photo',
            'undo_until': _undo_until(data)}


def list_captures(registry: SqliteProjectRegistry, state: str) -> Outcome:
    with registry._transaction() as db:
        found = [(json.loads(row[1]), row[0], row[2]) for row in db.execute('SELECT rowid, data, length(photo) FROM drive_captures')]
        people = dict(db.execute('SELECT telegram_id, id FROM persons WHERE telegram_id IS NOT NULL'))
    found = [item for item in found if item[0]['state'] == state]
    found.sort(key=lambda item: (datetime.fromisoformat(item[0]['posted_at']), item[1]), reverse=True)
    return Done(result={'captures': [
        {'capture': data['capture'], 'state': data['state'], 'posted_at': data['posted_at'], 'text': data.get('text'),
         'sender_telegram_id': data['sender_telegram_id'], 'sender': people.get(data['sender_telegram_id']),
         'lines': data['lines'], 'differences': data['differences'],
         'photo': {'size': size, 'media_type': data['media_type']} if size else None}
        for data, _, size in found]})


def capture_photo(registry: SqliteProjectRegistry, capture: str) -> Outcome:
    with registry._transaction() as db:
        row = db.execute('SELECT data, photo FROM drive_captures WHERE id=?', (capture,)).fetchone()
    if row is None or not row[1]:
        return Refused(rule='capture', reason='Capture or photo not found')
    return Done(result={'capture': capture, 'media_type': json.loads(row[0])['media_type'], 'size': len(row[1]),
                        'photo': base64.b64encode(row[1]).decode()})


def capture_result(registry: SqliteProjectRegistry, capture: str) -> Outcome:
    with registry._transaction() as db:
        row = db.execute('SELECT data FROM drive_captures WHERE id=?', (capture,)).fetchone()
        return Done(result=_result(json.loads(row[0]))) if row else Refused(rule='capture', reason='Capture not found')


class CaptureResolver:
    def __init__(self, registry: SqliteProjectRegistry, provider: Provider, *, decisions: Provider | None = None, environ: Mapping[str, str] | None = None,
                 debug: DebugNotifier | None = None) -> None:
        self.registry, self.provider, self.environ = registry, provider, environ
        self.decisions = decisions
        self.debug = debug

    def refresh_debug(self, job: WorkerJob) -> JsonValue:
        """`drive.capture.debug`: re-render a Capture's debug message after a change made through the API (undo)."""
        if self.debug is not None:
            self.debug.update(str(job.payload['capture']), None)
        return None

    def _note(self, capture: str, caption: str, facts: dict[str, Any] | None = None, error: str | None = None) -> None:
        if self.debug is not None:
            self.debug.update(capture, caption, facts, error)

    def handle(self, job: WorkerJob) -> JsonValue:
        capture, seen = str(job.payload['capture']), dict[str, str]()
        try:
            result = self._handle(job, capture, seen)
        except Exception as error:
            self._note(capture, seen.get('caption', ''), error=f'{type(error).__name__}: {error}')
            raise
        if 'caption' in seen:
            self._note(capture, seen['caption'])
        return result

    def _handle(self, job: WorkerJob, capture: str, seen: dict[str, str]) -> JsonValue:
        with self.registry._transaction() as db:
            row = db.execute('SELECT data, photo FROM drive_captures WHERE id=?', (capture,)).fetchone()
            if row is None:
                return {'skipped': 'capture removed by reload'}
            data = json.loads(row[0])
            if data['state'] != 'pending' or data.get('revision', 0) != job.payload.get('revision', 0):
                return _result(data)
            admitted = recording_input(data['chat_id'], data['text'], row[1], recording_chat_id=recording_chat(self.environ),
                                       topic_id=data.get('topic_id'), recording_topic_id=recording_topic(self.environ))
            if admitted is None:
                raise ValueError('Capture no longer belongs to the configured recording channel')
        seen['caption'] = admitted.text
        self._note(capture, admitted.text, {'errors': [], 'parsed': None, 'photo': None})
        picked = data.get('follow_up_drive')  # the custodian chose the Drive: the photo is not read again
        # Jev runs once, first, on the withheld caption and has-photo flag; follow-ups, picks and caption edits skip it.
        judged = judge_transmittal(admitted.text, bool(admitted.photo), provider=self.decisions) if self.decisions is not None and not picked and not data.get('answered') and not data.get('edited') else None
        verdict = judged.verdict if judged else 'transmittal'
        if judged:
            self._note(capture, admitted.text, {'jev': {'choice': judged.choice, 'probabilities': judged.probabilities, 'model': judged.model,
                                                        'ms': judged.latency_ms, 'failed': judged.failed}}, f'Jev failed: {judged.failed}' if judged.failed else None)
        readings: list[Any] = []
        if not (picked or verdict == 'not_transmittal' or not admitted.photo):
            started = time.monotonic()
            read = read_drive_photo(Image(admitted.photo, ImageType(data['media_type'])), provider=self.provider)
            readings = read.drives
            self._note(capture, admitted.text, {'photo': {'readings': [{'label': r.label, 'serial': r.serial, 'confidence': r.confidence} for r in readings],
                                                         'model': read.trace.model, 'ms': read.trace.latency_ms or round((time.monotonic() - started) * 1000)}},
                       f'Photo read failed: {read.reason}' if read.reason not in ('', 'unreadable') else None)
        with self.registry._transaction() as db:
            row = db.execute('SELECT data FROM drive_captures WHERE id=?', (capture,)).fetchone()
            if row is None:
                return {'skipped': 'capture removed by reload'}
            data = json.loads(row[0])
            if data['state'] != 'pending' or data.get('revision', 0) != job.payload.get('revision', 0):
                return _result(data)
            if verdict == 'not_transmittal':
                data['state'] = 'ignored'
                data.pop('text', None)
                data.pop('follow_up', None)
                db.execute('UPDATE drive_captures SET data=?, photo=? WHERE id=?', (json.dumps(data), b'', capture))
                return _result(data)
            store = SqliteDriveStore(self.registry, within=db)
            drives = store.list_drives()
            found = {}
            direction, person, place, typed = _parse_text(data['text'])
            follow_up = data.get('follow_up') or ''
            if (answer := _parse_text(follow_up))[0] is not None and not picked:  # an IN/OUT answer replaces the caption's direction
                direction, person, place, typed = answer[0], answer[1], answer[2], answer[3] or typed
                follow_up = answer[3]
            if self.debug is not None:
                data.setdefault('debug', {})['parsed'] = {'direction': direction, 'person': person or None, 'place': place}
            if not admitted.photo and not picked and not follow_up:
                data['state'], data['reason'] = 'exception', 'no_photo'
                db.execute('UPDATE drive_captures SET data=? WHERE id=?', (json.dumps(data), capture))
                return _result(data)
            if direction is None or direction == 'OUT' and not person:
                data['state'], data['reason'] = 'exception', 'missing_direction' if readings or typed or follow_up else 'unreadable_label'
                db.execute('UPDATE drive_captures SET data=? WHERE id=?', (json.dumps(data), capture))
                return _result(data)
            for item in readings:
                candidates = [d for d in drives if item.label and d.label == item.label]
                drive = candidates[0] if len(candidates) == 1 else store.find_by_serial(item.serial) if item.serial else None
                if drive:
                    found[drive.id] = drive
                    typed_drive = store.find_by_serial(typed) if typed else None
                    if typed and item.label and typed != item.label and (typed_drive is None or typed_drive.id != drive.id):
                        data['differences'].append(f'Photo label {item.label} differs from typed {typed}')
            if picked and (chosen := store.get_drive(picked)) is not None:
                found[chosen.id] = chosen
            elif not found and (not readings or follow_up):
                identifier = follow_up or typed
                candidates = [d for d in drives if identifier and d.label == identifier]
                drive = candidates[0] if len(candidates) == 1 else store.find_by_serial(identifier) if identifier else None
                if drive:
                    found[drive.id] = drive
            recorder = db.execute('SELECT id FROM persons WHERE telegram_id=?', (data['sender_telegram_id'],)).fetchone()
            logged_by = Ref(page_id=recorder[0]) if recorder else Ref(text=f"telegram:{data['sender_telegram_id']}")
            if direction == 'IN':
                person = recorder[0] if recorder else f"telegram:{data['sender_telegram_id']}"
            for drive in found.values():
                assignments = [a for a in store.list_assignments(drive.id) if a.end is None]
                project = assignments[0].project if len(assignments) == 1 and assignments[0].project else None
                movement = Movement(id='t6m_' + uuid4().hex, drive=drive.id, label=drive.label,
                                    on=datetime.fromisoformat(data['posted_at']).date(), direction=direction, person=person,
                                    recorder=recorder[0] if recorder else None, recorder_telegram_id=data['sender_telegram_id'],
                                    place=place, project=project, capture=capture, posted_at=data['posted_at'],
                                    shipment_id='shipment-' + capture if direction == 'OUT' else None)
                append(db, movement)
                event = custody_event(movement, logged_by)
                _append_event(db, store, drive.id, event, movement.id)
                if direction == 'OUT' or place is not None:
                    store.set_facts(drive.id, {'office_location': place if direction == 'IN' else None})
                latest = latest_custody(db, drive.id)
                if latest and latest != event:
                    _append_event(db, store, drive.id, latest, movement.id)
                data['lines'].append({'label': drive.label, 'direction': direction, 'person': person})
            data['state'] = 'resolved' if found else 'exception'
            if found:
                data['resolved_at'] = self.registry._clock()
            data['reason'] = None if found else 'no_drive_match' if readings else 'unreadable_label'
            db.execute('UPDATE drive_captures SET data=? WHERE id=?', (json.dumps(data), capture))
            return _result(data)


def undo_capture(registry: SqliteProjectRegistry, capture: str) -> Outcome:
    from t6.drives import CustodyKind

    with registry._transaction() as db:
        row = db.execute('SELECT data FROM drive_captures WHERE id=?', (capture,)).fetchone()
        if row is None:
            return Refused(rule='capture', reason='Capture not found')
        data = json.loads(row[0])
        if data['state'] in ('undone', 'ignored'):
            return Done(result=_result(data))
        if data['state'] == 'resolved' and data.get('resolved_at') is not None and registry._clock() - data['resolved_at'] > UNDO_WINDOW_SECONDS:
            return Refused(rule='undo_window', reason='A capture can be undone only within one hour of being resolved')
        store = SqliteDriveStore(registry, within=db)
        active = active_movements(db)
        for original in [m for m in active if m.capture == capture]:
            assert original.drive is not None
            previous = latest_custody(db, original.drive, exclude_capture=capture)
            reversal = replace(original, id='t6m_' + uuid4().hex, corrects=original.id,
                               direction='OUT' if previous and previous.kind is CustodyKind.RELEASED else 'IN',
                               person=previous.party.ref.text if previous and previous.party else None,
                               remarks='Capture undone', shipment_id=previous.shipment_id if previous else None)
            append(db, reversal)
            _append_event(db, store, original.drive, previous or custody_event(reversal, Ref(text='capture undo')), reversal.id)
            if previous is None:
                # A reversal to no earlier custody must restore unknown, not IN.
                db.execute('INSERT INTO drive_status_resets VALUES (last_insert_rowid())')
        data['state'] = 'undone'
        db.execute('UPDATE drive_captures SET data=? WHERE id=?', (json.dumps(data), capture))
        if data.get('debug', {}).get('message_ref') is not None:  # the worker keeps the debug message current
            InternalJobQueue(registry).submit('main', 'drive.capture.debug', f'{capture}:undone', {'capture': capture}, db=db)
        return Done(result=_result(data))
