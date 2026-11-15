"""Project committed Deliverables into Notion without retrying an uncertain create."""
from datetime import date
from typing import Any, cast
from urllib.parse import unquote

from t6.notion import codec
from t6.notion.editor import editor as notion_editor
from t6.registry import normalize_page_id

from t6.notion.generated import DeliverablesRow
from t6.notion.writes import Ambiguous, Rejected, create_row
from t6.worker.runner import Refused


def projection_id() -> str:
    property_id = DeliverablesRow.notion_id('t6_id')
    if not property_id:
        raise Refused('Deliverable T6 ID property ID is missing; create the property and run t6 admin schema-snapshot, then regenerate models')
    return property_id


class DeliverableProjection:
    def __init__(self, notion: Any, registry: Any, data_source: str) -> None:
        self.notion, self.registry, self.data_source = notion, registry, data_source

    def reconcile(self, job: Any) -> dict[str, Any]:
        page_id = str(job.payload['page_id'])
        page = self.notion.retrieve_page(page_id)
        if normalize_page_id(str((page.get('parent') or {}).get('data_source_id', ''))) != normalize_page_id(self.data_source):
            return {'outcome': 'not_a_deliverable_page'}
        property_id = projection_id()
        properties = page.get('properties') or {}
        prop = next((value for value in properties.values() if unquote(str(value.get('id', ''))) == unquote(property_id)), None)
        changed = job.payload.get('updated_properties')
        updated = isinstance(changed, list) and any(unquote(str(value)) == unquote(property_id) for value in changed)
        editor, editor_id = notion_editor(self.notion, job)
        result = self.registry.observe_deliverable(page_id, {'t6_id': codec.rich_text(prop)}, ['t6_id'] if updated else [], editor, str(job.payload.get('event_id') or job.dedupe_key), editor_id=editor_id)
        if result.get('restore', {}).get('t6_id'):
            self.notion.update_page(page_id, {'properties': {property_id: {'rich_text': [{'text': {'content': result['restore']['t6_id']}}]}}})
            self.registry.projection(page_id, {'t6_id': result['restore']['t6_id']}, record=True)
        return cast(dict[str, Any], result)

    def sync(self, job: Any) -> dict[str, Any]:
        property_id = projection_id()
        intent = int(job.payload['intent_id'])
        state = self.registry.deliverable_sync_state(intent)
        if not state or state['done']:
            return {'skipped': 'completed or missing intent'}
        identifier = state['id']
        page = state['page_id']
        if not page:
            matches = self.notion.query_data_source(self.data_source, filter={'property': property_id, 'rich_text': {'equals': identifier}})
            if len(matches) > 1:
                self.registry.report_deliverable_sync(intent, error='Multiple Notion pages have this T6 ID')
                raise Refused('Multiple Notion pages have this T6 ID')
            if matches:
                page = str(matches[0]['id'])
            else:
                if not state['project_page']:
                    raise ValueError('Project has no active Notion page')
                if not self.registry.begin_deliverable_create(intent):
                    raise Refused('Create outcome unknown; inspect Notion before retrying')
                values = {'name': state['code'], 'projects': (state['project_page'],)}
                for src, dest in (('due', 'deadline'), ('air', 'playdate')):
                    if state[src]:
                        values[dest] = date.fromisoformat(state[src])
                saved = create_row(self.notion, DeliverablesRow, self.data_source, values,
                                   extra_properties={property_id: {'rich_text': [{'text': {'content': identifier}}]}})
                if isinstance(saved, (Rejected, Ambiguous)):
                    self.registry.report_deliverable_sync(intent, error=str(saved))
                    raise Refused(f'Notion create needs review: {saved}')
                page = saved.page_id
        self.notion.update_page(page, {'properties': {property_id: {'rich_text': [{'text': {'content': identifier}}]}}})
        self.registry.report_deliverable_sync(intent, page_id=page)
        return {'deliverable_id': identifier, 'page_id': page}
