"""Show each Person's T6 ID on its Contacts page, and put back an edit of it.

The registry owns the value (`person_sync_intents`, one per change of the bound page). The
handlers never create a Contacts page and never change another property.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import unquote

from t6.domain.events import JsonValue
from t6.notion import codec
from t6.notion.editor import editor as notion_editor
from t6.notion.generated import ContactsRow
from t6.notion.source import NotionSource, as_source
from t6.notion.sync import NotionClient
from t6.registry import normalize_page_id
from t6.worker.client import WorkerJob
from t6.worker.registry import RegistryClient
from t6.worker.runner import Handler, Refused


def projection_id() -> str:
    property_id = ContactsRow.notion_id('t6_id')
    if not property_id:
        raise Refused('Contacts T6 ID property ID is missing; create the property and run t6 admin snapshot, then regenerate models')
    return property_id


class PersonProjection:
    def __init__(self, notion: NotionSource | NotionClient, registry: RegistryClient, data_source: str) -> None:
        self.notion, self.registry, self.data_source = as_source(notion), registry, data_source

    def handlers(self) -> dict[str, Handler]:
        return {'person.sync': self.sync}

    def sync(self, job: WorkerJob) -> JsonValue:
        property_id = projection_id()
        intent = int(job.payload['intent_id'])  # type: ignore[arg-type]
        state = self.registry.person_sync_state(intent)
        if not state or state['done']:
            return {'skipped': 'completed or missing intent'}
        page = state['page_id']
        if not page:
            raise ValueError('Person has no active Contacts page')
        if ContactsRow.from_page(self.notion.retrieve_page(page, freshness='live')).t6_id != state['id']:
            self.notion.update_page(page, {'properties': {property_id: {'rich_text': [{'text': {'content': state['id']}}]}}})
        self.registry.report_person_sync(intent)
        return {'person_id': state['id'], 'page_id': page}

    def reconcile(self, job: WorkerJob) -> JsonValue:
        page_id = str(job.payload['page_id'])
        page = self.notion.retrieve_page(page_id, freshness='live')
        if normalize_page_id(str((page.get('parent') or {}).get('data_source_id', ''))) != normalize_page_id(self.data_source):
            return {'outcome': 'not_a_contacts_page'}
        property_id = projection_id()
        shown = next((value for value in (page.get('properties') or {}).values() if unquote(str(value.get('id', ''))) == unquote(property_id)), None)
        updated = job.payload.get('updated_properties')
        changed = ['t6_id'] if isinstance(updated, list) and any(unquote(str(value)) == unquote(property_id) for value in updated) else []
        editor, editor_id = notion_editor(self.notion, job)
        result: dict[str, Any] = self.registry.observe_person(page_id, {'t6_id': codec.rich_text(shown)}, changed, editor, str(job.payload.get('event_id') or job.dedupe_key), editor_id=editor_id)
        restore = result.get('restore', {})
        if restore.get('t6_id'):
            self.notion.update_page(page_id, {'properties': {property_id: {'rich_text': [{'text': {'content': restore['t6_id']}}]}}})
        return result
