"""The debug topic: one plain-text message per Capture, posted through the gateway and edited as steps finish.

The message is rebuilt from the Capture's data plus `data['debug']`, the facts the resolver notes (Jev, photo
reading, parse, errors, the gateway `message_ref`). Nothing is kept beyond what the Capture already holds: the
caption is withheld each time it is shown, and the photo is never sent. A failure here is logged and never raised.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from t6.privacy import withhold
from t6.registry.sqlite import SqliteProjectRegistry
from t6.worker.gateway import GatewayClient

log = logging.getLogger(__name__)
LIMIT = 3900  # Telegram allows 4096 characters
UNDO_WINDOW_SECONDS = 3600


def message_link(chat_id: int, topic_id: int | None, message_id: int) -> str:
    chat = str(chat_id)
    chat = chat[4:] if chat.startswith('-100') else chat.lstrip('-')
    return f'https://t.me/c/{chat}/' + (f'{topic_id}/' if topic_id is not None else '') + str(message_id)


def _marker(data: Mapping[str, Any], errors: list[str]) -> str:
    if errors:
        return '⛔ error'
    return {'resolved': '✅ resolved', 'exception': '⚠️ exception', 'ignored': '⚠️ ignored', 'undone': '⚠️ undone'}.get(data['state'], '… working')


def render(data: Mapping[str, Any], *, person: str | None, caption: str | None) -> str:
    """The message text, escaped for the gateway's HTML format."""
    facts: Mapping[str, Any] = data.get('debug', {})
    errors: list[str] = facts.get('errors', [])
    lines = [_marker(data, errors), f"Capture {data['capture']} (revision {data.get('revision', 0)})",
             f"Poster: Telegram {data['sender_telegram_id']}, Person {person or 'unknown'}",
             'Message: ' + message_link(data['chat_id'], data.get('topic_id'), data['message_id']),
             'Local check: passed',
             'Caption: ' + (withhold(caption).text if caption is not None else '(not kept)')]
    if data.get('edited'):
        lines.append('Caption was edited')
    if data.get('answered'):
        lines.append('Follow-up answer: ' + (withhold(data['follow_up']).text if data.get('follow_up') else '') + (f" (Drive {data['follow_up_drive']})" if data.get('follow_up_drive') else ''))
    if jev := facts.get('jev'):
        probabilities = jev.get('probabilities')
        lines.append(f"Jev: {jev.get('choice') or 'no answer (' + str(jev.get('failed')) + ')'}"
                     + (' match {match:.2f} maybe {maybe:.2f} no {no:.2f}'.format(**probabilities) if probabilities else '')
                     + f" [{jev.get('model')}, {jev.get('ms')} ms]")
    else:
        lines.append('Jev: not run')
    if photo := facts.get('photo'):
        read = '; '.join(f"label {r['label']} serial {r['serial']} confidence {r['confidence']}" for r in photo['readings']) or 'nothing readable'
        lines.append(f"Photo read: {read} [{photo['model']}, {photo['ms']} ms]")
    if parsed := facts.get('parsed'):
        lines.append('Parsed: direction {direction}, person {person}, place {place}'.format(**parsed))
    lines.append('Matched drives: ' + (', '.join(str(line['label']) for line in data['lines']) or 'none'))
    lines.append(f"State: {data['state']}" + (f", reason {data['reason']}" if data.get('reason') else ''))
    for difference in data.get('differences', []):
        lines.append('Difference: ' + difference)
    if data['state'] == 'resolved' and data.get('resolved_at') is not None:
        lines.append('Undo until: ' + datetime.fromtimestamp(data['resolved_at'] + UNDO_WINDOW_SECONDS, UTC).isoformat())
    if data['state'] == 'undone':
        lines.append('Undone')
    lines.extend('Error: ' + error for error in errors)
    return html.escape(withhold('\n'.join(lines)).text, quote=False)[:LIMIT]


class DebugNotifier:
    def __init__(self, registry: SqliteProjectRegistry, gateway: GatewayClient, chat: int, topic: int | None,
                 enabled: Callable[[], bool]) -> None:
        self.registry, self.gateway, self.chat, self.topic, self.enabled = registry, gateway, chat, topic, enabled

    def update(self, capture: str, caption: str | None, facts: Mapping[str, Any] | None = None, error: str | None = None) -> None:
        """Note `facts` (and one `error`) on the Capture, then post or edit its message. Never raises."""
        try:
            if not self.enabled():
                return
            self._update(capture, caption, facts or {}, error)
        except Exception:
            log.exception('debug topic update failed for %s', capture)

    def _update(self, capture: str, caption: str | None, facts: Mapping[str, Any], error: str | None) -> None:
        with self.registry._transaction() as db:
            row = db.execute('SELECT data FROM drive_captures WHERE id=?', (capture,)).fetchone()
            if row is None:
                return
            data = json.loads(row[0])
            noted = data.setdefault('debug', {})
            noted.update(facts)
            if error:
                noted.setdefault('errors', []).append(error[:300])
            person = db.execute('SELECT id FROM persons WHERE telegram_id=? AND active=1', (data['sender_telegram_id'],)).fetchone()
            text = render(data, person=person[0] if person else None, caption=caption if caption is not None else data.get('text'))
            digest = hashlib.sha256(text.encode()).hexdigest()[:16]
            ref = noted.get('message_ref')
            db.execute('UPDATE drive_captures SET data=? WHERE id=?', (json.dumps(data), capture))
            if ref is not None and noted.get('digest') == digest:
                return
        if ref is None:  # the idempotency key makes a repeat after a crash return the same message
            ref = self.gateway.notify(self.chat, text, [], f'drive-debug:{capture}', self.topic)
        else:
            self.gateway.edit(ref, text, [], f'drive-debug:{capture}:{digest}')
        with self.registry._transaction() as db:
            row = db.execute('SELECT data FROM drive_captures WHERE id=?', (capture,)).fetchone()
            if row is not None:
                data = json.loads(row[0])
                data.setdefault('debug', {}).update(message_ref=ref, digest=digest)
                db.execute('UPDATE drive_captures SET data=? WHERE id=?', (json.dumps(data), capture))
