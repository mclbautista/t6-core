"""Project committed Job Order sync intents into Notion."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from t6.domain.events import JsonValue
from t6.notion.generated import JobOrdersRow
from t6.notion.job_orders import JobOrders
from t6.notion.model import encode
from t6.notion.sync import NotionClient
from t6.notion.writes import Ambiguous, Rejected
from t6.worker.client import WorkerJob
from t6.worker.registry import RegistryClient
from t6.worker.runner import Handler, Refused


class JobOrderProjection:
    def __init__(self, notion: NotionClient, registry: RegistryClient, data_source: str) -> None:
        self.notion, self.registry, self.data_source = notion, registry, data_source

    def handlers(self) -> dict[str, Handler]:
        return {'jo.sync': self.sync}

    def sync(self, job: WorkerJob) -> JsonValue:
        intent_id = int(job.payload['intent_id'])  # type: ignore[arg-type]
        state = self.registry.jo_sync_state(intent_id)
        if state is None or state['done']:
            return {'skipped': 'completed or missing intent'}
        jo_id = state['jo_id']
        page = state['page_id']
        if not page and state['operation'] == 'create':
            matches = self.notion.query_data_source(self.data_source, filter={'property': 'T6 ID', 'rich_text': {'equals': jo_id}})
            if len(matches) > 1:
                self.registry.report_jo_sync(intent_id, error='Multiple Notion pages have this T6 ID')
                raise Refused('Multiple Notion pages have this T6 ID')
            if matches:
                page = str(matches[0]['id'])
            else:
                if not state['project_page']:
                    raise ValueError('Project has no active Notion page')
                if not self.registry.begin_jo_create(intent_id):
                    self.registry.report_jo_sync(intent_id, error='Create outcome unknown; inspect Notion before retrying')
                    raise Refused('Create outcome unknown; inspect Notion before retrying')
                fields: dict[str, Any] = dict(state['fields'])
                fields['project_id'] = state['project_page']
                for name in ('date', 'deadline'):
                    if isinstance(fields.get(name), str):
                        raw = fields[name]
                        fields[name] = datetime.fromisoformat(raw) if 'T' in raw or ' ' in raw else date.fromisoformat(raw)
                saved = JobOrders(self.notion, self.data_source).create(fields, t6_id=jo_id)
                if isinstance(saved, Rejected):
                    self.registry.report_jo_sync(intent_id, error=str(saved))
                    raise Refused(f'Notion create needs review: {saved}')
                if isinstance(saved, Ambiguous):
                    found = self.notion.query_data_source(self.data_source, filter={'property': 'T6 ID', 'rich_text': {'equals': jo_id}})
                    if len(found) != 1:
                        self.registry.report_jo_sync(intent_id, error=str(saved))
                        raise Refused(f'Notion create needs review: {saved}')
                    page = str(found[0]['id'])
                else:
                    page = saved.page_id
        if not page:
            raise ValueError('Job Order page has not been created yet')
        body = state['fields'].get('source_text')
        if body:
            failure = JobOrders(self.notion, self.data_source).ensure_body(page, str(body))
            if failure is not None:
                self.registry.report_jo_sync(intent_id, page_id=page, error=str(failure))
                raise Refused(f'Notion body needs review: {failure}')
        properties: dict[str, Any] = {'T6 ID': {'rich_text': [{'text': {'content': jo_id}}]}}
        if 'status' in state['payload']:
            properties.update(JobOrdersRow.to_properties(status=state['payload']['status']))
        if state['assignee_id']:
            if not state['assignee_page']:
                raise ValueError('Assignee has no active Contacts page')
            properties.update(JobOrdersRow.to_properties(assigned=(state['assignee_page'],)))
        if state['notes']:
            properties['T6 Notes'] = encode('rich_text', '\n'.join(state['notes']))
        self.notion.update_page(page, {'properties': properties})
        self.registry.report_jo_sync(intent_id, page_id=page)
        return {'jo_id': jo_id, 'page_id': page}
