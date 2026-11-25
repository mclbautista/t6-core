"""Custodian Item messages through the gateway; no target means no delivery."""
from __future__ import annotations

import base64
import html
import hashlib
import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from t6.domain.events import JsonValue
from t6.drives.debug import message_link
from t6.drives.movements import latest_custody
from t6.drives.practice import practice_mode
from t6.drives.sqlite import SqliteDriveStore
from t6.registry.sqlite import SqliteProjectRegistry
from t6.worker import WorkerJob
from t6.worker.gateway import GatewayClient
from t6.worker.internal_queue import InternalJobQueue

KIND = 'drive.item.notify'


def dm_target(registry: SqliteProjectRegistry, environ: Mapping[str, str]) -> tuple[int, int | None] | None:
    if practice_mode(registry):
        chat = environ.get('T6_DRIVE_DM_CHAT_ID')
        topic = environ.get('T6_DRIVE_DM_TOPIC_ID')
        return (int(chat), int(topic) if topic else None) if chat else None
    with registry._transaction() as db:
        row = db.execute('SELECT telegram_id FROM persons WHERE id=? AND active=1',
                         (environ.get('T6_DRIVE_CUSTODIAN_PERSON_ID'),)).fetchone()
    return (int(row[0]), None) if row and row[0] and int(row[0]) > 0 else None


def queue_item(registry: SqliteProjectRegistry, capture: str, item: int, version: str, *, db: sqlite3.Connection) -> None:
    InternalJobQueue(registry).submit('main', KIND, f'{capture}:{item}:{version}', {'capture': capture, 'item': item}, db=db)


class ItemNotifier:
    def __init__(self, registry: SqliteProjectRegistry, gateway: GatewayClient, environ: Mapping[str, str]) -> None:
        self.registry, self.gateway, self.environ = registry, gateway, environ

    def handle(self, job: WorkerJob) -> JsonValue:
        capture, number = str(job.payload['capture']), int(str(job.payload['item']))
        target = dm_target(self.registry, self.environ)
        if target is None:
            return {'skipped': 'no DM target'}
        practice = practice_mode(self.registry)
        with self.registry._transaction() as db:
            row = db.execute('SELECT data, photo FROM drive_captures WHERE id=?', (capture,)).fetchone()
            if row is None:
                return {'skipped': 'capture removed by reload'}
            data = json.loads(row[0])
            item = data['items'][number]
            ref = item.get('dm_message_ref')
            store = SqliteDriveStore(self.registry, within=db)
            outcome = item.get('last_approval') or ('undone' if data['state'] == 'undone' else None)
            if ref is not None:
                if outcome is None:
                    return {'message_ref': ref}
                drive = store.get_drive(item['drive']) if item.get('drive') else None
                label = (drive.label or drive.serial or drive.id) if drive else ''
                decision = {'none': 'None — waiting in Inbox', 'not_a_drive': 'Not a drive', 'undone': 'Undone'}.get(outcome, 'Approved: ' + label)
                text = html.escape(('Practice\n' if practice else '') + item['text'] + '\n' + decision, quote=False)
                digest = hashlib.sha256(text.encode()).hexdigest()[:16]
                if item.get('dm_digest') == digest:
                    return {'message_ref': ref}
            else:
                if data['state'] != 'resolved' or item['state'] != 'open' or outcome:
                    return {'skipped': 'Item settled'}
                first: dict[str, Any] = next(iter(item.get('guesses', [])), {})
                if self.environ.get('T6_DRIVE_AUTO_APPROVE', '').strip().lower() in {'1', 'true', 'yes'} and first.get('exact') and first.get('confidence') == 0.95:
                    return {'skipped': 'exact guess waits in Inbox'}
                text, buttons = self._message(data, number, store, db, practice)
                photo = base64.b64encode(row[1]).decode() if row[1] else None
                digest = None
        if ref is None:
            ref = self.gateway.notify(target[0], text, buttons, f'drive-item:{capture}:{number}', target[1], photo=photo)
        else:
            self.gateway.edit(ref, text, [], f'drive-item-edit:{capture}:{number}:{digest}')
        with self.registry._transaction() as db:
            row = db.execute('SELECT data FROM drive_captures WHERE id=?', (capture,)).fetchone()
            if row:
                data = json.loads(row[0])
                data['items'][number]['dm_message_ref'] = ref
                data['items'][number]['dm_digest'] = digest
                db.execute('UPDATE drive_captures SET data=? WHERE id=?', (json.dumps(data), capture))
                if digest is None and (data['items'][number].get('last_approval') or data['state'] == 'undone'):
                    queue_item(self.registry, capture, number, 'settled-during-send', db=db)
        return {'message_ref': ref}

    def _message(self, data: dict[str, Any], number: int, store: SqliteDriveStore, db: sqlite3.Connection, practice: bool) -> tuple[str, list[list[dict[str, str]]]]:
        item = data['items'][number]
        lines = (['Practice'] if practice else []) + [
            f"Item: {item['text']}", message_link(data['chat_id'], data.get('topic_id'), data['message_id']), data['text']]
        buttons: list[list[dict[str, str]]] = []
        prefix = f"drive:{data['capture'].removeprefix('capture_')}:{number}:"
        codes = dict(db.execute('SELECT id, code FROM projects WHERE code IS NOT NULL'))
        for rank, guess in enumerate(item.get('guesses', [])[:3], 1):
            drive = store.get_drive(guess['drive'])
            assert drive is not None
            projects = [a.project.text or codes.get(a.project.page_id) or a.project.page_id or 'Unknown Project' for a in store.list_assignments(drive.id) if a.end is None and a.project]
            label = ('✓ ' if guess.get('exact') else '') + (drive.label or drive.serial or drive.id)
            project = ', '.join(dict.fromkeys(projects)) or 'Unknown Project'
            last = latest_custody(db, drive.id, exclude_capture=data['capture'])
            last_seen = f'{last.kind.value} — {last.on or "Unknown date"}' if last else 'Unknown'
            if last and last.party:
                last_seen += ' — ' + (last.party.ref.text or last.party.ref.page_id or 'Unknown person')
            lines += [f'{rank}. {label} — {project}', f'Last seen: {last_seen}']
            buttons.append([{'text': f'{label} — {project}', 'payload': prefix + f'g{rank}'}])
        buttons.extend([[{'text': label, 'payload': prefix + choice}] for label, choice in (
            ('New drive', 'new'), ('Not a drive', 'skip'), ('Type it', 'type'), ('None', 'none'))])
        return html.escape('\n'.join(lines), quote=False), buttons


def tap_inputs(registry: SqliteProjectRegistry, data: str) -> dict[str, Any] | None:
    """Decode a compact gateway button into the existing `drive approve` input."""
    import re
    matched = re.fullmatch(r'drive:([0-9a-f]{32}):(\d+):(g[123]|new|skip|none|type)', data)
    if matched is None:
        return None
    capture, number, choice = 'capture_' + matched[1], int(matched[2]), matched[3]
    with registry._transaction() as db:
        row = db.execute('SELECT data FROM drive_captures WHERE id=?', (capture,)).fetchone()
    if row is None:
        return None
    items = json.loads(row[0]).get('items', [])
    if number >= len(items):
        return None
    values: dict[str, Any] = {'capture': capture, 'item': number}
    if choice == 'type':
        if items[number]['state'] != 'open':
            return None
        values['choice'] = 'drive'
    elif choice.startswith('g'):
        guesses = items[number].get('guesses', [])
        rank = int(choice[1]) - 1
        if rank >= len(guesses):
            return None
        values.update(choice='drive', drive=guesses[rank]['drive'])
    else:
        values['choice'] = 'not_a_drive' if choice == 'skip' else choice
    return values
