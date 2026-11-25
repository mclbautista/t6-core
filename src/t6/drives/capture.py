"""Durable recording posts, resolved asynchronously after admission."""
from __future__ import annotations

import base64
import binascii
import json
import os
from collections.abc import Mapping
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from t6.domain.events import JsonValue
from t6.drives import Ref
from t6.drives.caption import parse_caption
from t6.drives.inbox import _move
from t6.drives.movements import active_movements, append, custody_event, latest_custody, _append_event
from t6.drives.debug import DebugNotifier
from t6.drives.sqlite import SqliteDriveStore
from t6.drives.shortlist import guesses, mark_exact
from t6.drives.learning import reference_photo
from t6.drives.photos import PhotoError
from t6.llm.drive_transmittal import judge_transmittal
from t6.llm.transport import Provider
from t6.llm.drive_photo_match import PHOTO_MATCH, MatchInput, shrink_photo
from t6.llm.task import run
from t6.llm.result import Ok
from t6.privacy import withhold
from t6.notion.source import NotionSource
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


def _notion_source(environ: Mapping[str, str]) -> NotionSource | None:
    from t6.notion import MirrorReader, NotionClient, mirror_db_path
    token = environ.get('T6_NOTION_TOKEN')
    if not token:
        return None
    path = mirror_db_path(environ)
    return NotionSource(lambda: NotionClient(token), mirror=MirrorReader(path) if path else None)


UNDO_WINDOW_SECONDS = 3600

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
                if not (data['state'] in ('pending', 'exception') or data['state'] == 'resolved' and parse_caption(data['text']).problem) or data.get('text') == inputs.text:
                    return Done(result=_result(data))
                data.pop('grade', None)
                data.update(text=inputs.text, revision=data.get('revision', 0) + 1, state='pending', reason=None, differences=[], edited=True, items=[])
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
    caption = parse_caption(data['text']) if data['state'] != 'ignored' and 'text' in data else None  # an ignored capture keeps no caption
    return {key: data[key] for key in ('capture', 'state', 'lines', 'differences')} | {
            'items': data.get('items') or [{'text': text, 'state': 'open'} for text in caption.items] if caption else [], 'counts': caption.counts if caption else [],
            'recipient': caption.recipient if caption else None, 'tags': caption.tags if caption else [],
            'grade': data.get('grade', caption.grade) if caption else None, 'problem': caption.problem if caption else None,'photo_reference': None if data['state'] == 'ignored' else data['capture'], 'message': 'ask for the label' if data['state'] == 'exception' else None,
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
    def __init__(self, registry: SqliteProjectRegistry, provider: Provider | None, *, decisions: Provider | None = None, environ: Mapping[str, str] | None = None,
                 debug: DebugNotifier | None = None, source: NotionSource | None = None) -> None:
        self.registry, self.provider, self.environ = registry, provider, environ
        self.decisions = decisions
        self.debug = debug
        self.source = source if source is not None else _notion_source(os.environ if environ is None else environ)

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
        from t6.drives.notifications import dm_target, queue_item
        notify = dm_target(self.registry, os.environ if self.environ is None else self.environ) is not None
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
        caption = parse_caption(data['text'])
        items: list[dict[str, Any]] = [{'text': text, 'state': 'open', 'guesses': []} for text in caption.items]
        matches = []
        for item in items:
            if admitted.photo and not caption.problem and caption.direction:
                item['guesses'], match = self._match_item(data, item['text'], admitted.photo)
                matches.append(match)
        with self.registry._transaction() as db:
            row = db.execute('SELECT data FROM drive_captures WHERE id=?', (capture,)).fetchone()
            if row is None:
                return {'skipped': 'capture removed by reload'}
            data = json.loads(row[0])
            if data['state'] != 'pending' or data.get('revision', 0) != job.payload.get('revision', 0):
                return _result(data)
            data.update(state='resolved', resolved_at=self.registry._clock(), reason=None, items=items)
            store = SqliteDriveStore(self.registry, within=db)
            for item in items:
                if item['guesses']:
                    drive = store.get_drive(item['guesses'][0]['drive'])
                    assert drive is not None
                    item.update(drive=drive.id, movement=_move(db, store, data, drive, self.source, to_review=True))
            if self.debug is not None:
                data.setdefault('debug', {})['matches'] = matches
            data['grade'] = 'thin' if not caption.problem and any(not item['guesses'] for item in items) else caption.grade
            db.execute('UPDATE drive_captures SET data=? WHERE id=?', (json.dumps(data), capture))
            if not caption.problem and notify:
                for number, _ in enumerate(items):
                    queue_item(self.registry, capture, number, 'resolved', db=db)
            return _result(data)

    def _match_item(self, data: dict[str, Any], text: str, photo: bytes) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        caption = parse_caption(data['text'])
        assert caption.direction is not None
        with self.registry._transaction() as db:
            store = SqliteDriveStore(self.registry, within=db)
            try:
                shortlist = guesses(db, store, text, caption.direction, caption.tags, self.source, data['sender_telegram_id'])
            except ImportError:
                shortlist = []
                failure = 'lookup_unavailable'
            else:
                failure = 'empty_shortlist' if not shortlist else 'no_provider'
            entries = [{'letter': chr(65 + n), **guess} for n, guess in enumerate(shortlist)]
            match: dict[str, Any] = {'item': text, 'shortlist': entries, 'thumbnails': [], 'guesses': [],
                                     'exact_checks': [], 'model': None, 'ms': None, 'cost': None,
                                     'failed': failure}
            if not shortlist or self.provider is None:
                return [], match
            try:
                image = shrink_photo(photo)
            except (ImportError, OSError, ValueError):
                match['failed'] = 'invalid_photo'
                return [], match
            thumbnails, letters = [], []
            for entry in entries:
                drive = store.get_drive(entry['drive'])
                assert drive is not None
                entry.update(label=drive.label, projects=[asdict(a.project) for a in store.list_assignments(drive.id) if a.end is None and a.project])
                try:
                    reference = reference_photo(self.registry, drive.id, within=db)
                    thumbnail = shrink_photo(base64.b64decode(reference['photo']), 320)
                except (PhotoError, OSError, ValueError):
                    entry['thumbnail'] = False
                else:
                    entry['thumbnail'] = True
                    thumbnails.append(thumbnail)
                    letters.append(withhold(entry['letter']))
        request = MatchInput(withhold(json.dumps({'item': text, 'caption': data['text'], 'shortlist': entries})),
                             image, tuple(thumbnails), tuple(letters))
        matched = run(PHOTO_MATCH, request, provider=self.provider)
        match.update(thumbnails=[letter.text for letter in letters], model=matched.trace.model,
                     ms=matched.trace.latency_ms, cost=matched.trace.cost, tokens=asdict(matched.trace.tokens), failed=getattr(matched, 'reason', None))
        if not isinstance(matched, Ok):
            return [], match
        if any(ord(g.letter) - 65 >= len(shortlist) for g in matched.value.guesses):
            match['failed'] = 'letter_outside_shortlist'
            return [], match
        ranked = [{'drive': shortlist[ord(g.letter) - 65]['drive'], 'confidence': g.confidence,
                   'reason': g.reason, 'exact': False} for g in matched.value.guesses]
        with self.registry._transaction() as db:
            store = SqliteDriveStore(self.registry, within=db)
            match['exact_checks'] = [{'text': text, 'candidates': mark_exact(db, store, shortlist, text)}]
            if matched.value.label_text:
                match['exact_checks'].append({'text': matched.value.label_text,
                                              'candidates': mark_exact(db, store, shortlist, matched.value.label_text)})
        exact = [g for g in shortlist if g['exact']]
        exact_ids = {g['drive'] for g in exact}
        ranked = (exact + sorted((g for g in ranked if g['drive'] not in exact_ids), key=lambda g: -g['confidence']))[:3]
        match['guesses'] = ranked
        return ranked, match


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
        for original in [m for m in active if m.capture == capture and m.drive is not None]:
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
        for item in data.get('items', []):
            if item['state'] == 'open':
                item['state'] = 'closed'
        db.execute('UPDATE drive_captures SET data=? WHERE id=?', (json.dumps(data), capture))
        if data.get('debug', {}).get('message_ref') is not None:  # the worker keeps the debug message current
            InternalJobQueue(registry).submit('main', 'drive.capture.debug', f'{capture}:undone', {'capture': capture}, db=db)
        return Done(result=_result(data))
