"""Project committed Deliverables into Notion without retrying an uncertain create."""
from datetime import date
from typing import Any, cast
from urllib.parse import unquote

from t6.notion import codec
from t6.notion.editor import editor as notion_editor
from t6.registry import normalize_page_id

from t6.notion.generated import DeliverablesRow
from t6.notion.named import live_option_name
from t6.notion.source import NotionSource, as_source
from t6.notion.sync import NotionClient
from t6.notion.writes import Ambiguous, Rejected, create_row
from t6.worker.runner import Refused


def projection_id() -> str:
    property_id = DeliverablesRow.notion_id('t6_id')
    if not property_id:
        raise Refused('Deliverable T6 ID property ID is missing; create the property and run t6 admin snapshot, then regenerate models')
    return property_id


class DeliverableProjection:
    def __init__(self, notion: NotionSource | NotionClient, registry: Any, data_source: str) -> None:
        self.notion, self.registry, self.data_source = as_source(notion), registry, data_source

    def _option(self, wanted: str) -> str:
        """The team's own Status option for `wanted`; the API never creates one."""
        try:
            return live_option_name(self.notion.retrieve_data_source(self.data_source, freshness='live'), DeliverablesRow.notion_id('status') or 'Status', wanted)
        except ValueError as error:
            raise Refused(str(error)) from error

    def reconcile(self, job: Any) -> dict[str, Any]:
        page_id = str(job.payload['page_id'])
        page = self.notion.retrieve_page(page_id, freshness='live')
        if normalize_page_id(str((page.get('parent') or {}).get('data_source_id', ''))) != normalize_page_id(self.data_source):
            return {'outcome': 'not_a_deliverable_page'}
        property_id = projection_id()
        properties = page.get('properties') or {}
        status_id = DeliverablesRow.notion_id('status') or 'Status'
        def prop(identifier: str) -> Any:
            return next((value for value in properties.values() if unquote(str(value.get('id', ''))) == unquote(identifier)), None)
        changed = job.payload.get('updated_properties')
        updated = [name for name, identifier in (('t6_id', property_id), ('status', status_id))
                   if isinstance(changed, list) and any(unquote(str(value)) == unquote(identifier) for value in changed)]
        editor, editor_id = notion_editor(self.notion, job)
        result = self.registry.observe_deliverable(page_id, {'t6_id': codec.rich_text(prop(property_id)), 'status': codec.select(prop(status_id))}, updated, editor, str(job.payload.get('event_id') or job.dedupe_key), editor_id=editor_id)
        restore = result.get('restore', {})
        writes: dict[str, Any] = {}
        if restore.get('t6_id'):
            writes[property_id] = {'rich_text': [{'text': {'content': restore['t6_id']}}]}
        if restore.get('status'):
            writes[status_id] = {'select': {'name': self._option(restore['status'])}}
        if writes:
            self.notion.update_page(page_id, {'properties': writes})
            values = {}
            if restore.get('t6_id'):
                values['t6_id'] = restore['t6_id']
            if restore.get('status'):
                values['deliverable_status'] = restore['status']
            self.registry.projection(page_id, values, record=True)
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
            matches = self.notion.query_data_source(self.data_source, filter={'property': property_id, 'rich_text': {'equals': identifier}}, freshness='live')
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
        status_id = DeliverablesRow.notion_id('status') or 'Status'
        current = self.notion.retrieve_page(page, freshness='live')
        current_status = DeliverablesRow.from_page(current).status
        properties: dict[str, Any] = {property_id: {'rich_text': [{'text': {'content': identifier}}]}}
        if DeliverablesRow.from_page(current).name != state['code']:
            properties[DeliverablesRow.notion_id('name') or 'title'] = {'title': [{'text': {'content': state['code']}}]}
        if current_status != state['status']:
            properties[status_id] = {'select': {'name': self._option(state['status'])}}
        self.notion.update_page(page, {'properties': properties})
        self.registry.projection(page, {'deliverable_status': state['status']}, record=True)
        self.registry.report_deliverable_sync(intent, page_id=page)
        return {'deliverable_id': identifier, 'page_id': page}
