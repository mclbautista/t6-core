"""Project Drive reads through HTTP verbs with synthetic captures and a fake provider."""
import base64
from datetime import date, datetime
import json
import os
import threading
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from test_drive_reload import DriveReloadFixture, HEADER
from test_llm_drive_photo import photo
from t6.drives.capture import CaptureResolver
from t6.ids import EntityKind, T6Id
from t6.llm.testing import FakeProvider
from t6.notion.generated import ProjectsRow
from t6.notion.testing import project_properties, relation
from t6.server.http import make_server
from t6.server.verbs import VerbAPI
from t6.worker import Worker
from t6.worker.internal_queue import InternalJobQueue, InternalQueueClient


class DriveProjectReadTests(DriveReloadFixture):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(patch.dict(os.environ, {'T6_DRIVE_RECORDING_CHAT_ID': '-900001', 'T6_DRIVE_RECORDING_TOPIC_ID': ''}))
        self.now = [datetime.fromisoformat('2026-11-25T08:00:00+08:00').timestamp()]
        self.registry._clock = lambda: self.now[0]
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('t6n_synthetickeeper', 'staff', '[]', '[]', 900010, 0, 1)")
            db.execute("INSERT INTO person_permissions VALUES ('t6n_synthetickeeper', 'drives')")
            db.execute("INSERT INTO persons VALUES ('t6n_syntheticreader', 'staff', '[]', '[]', NULL, 0, 1)")
        self.queue = InternalJobQueue(self.registry)
        self.fake = FakeProvider()
        api = VerbAPI(self.people, self.queue, {'synthetic-gateway': 'gateway', 'synthetic-keeper': 't6n_synthetickeeper',
                      'synthetic-reader': 't6n_syntheticreader'}, lambda _: None, notion_source=self.notion_source)
        self.server = make_server(('127.0.0.1', 0), api)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.sequence = 0

    def http(self, path: str, secret: str = 'synthetic-keeper', *, expected: str = 'done', **values: Any) -> dict[str, Any]:
        request = Request(f'http://127.0.0.1:{self.server.server_port}/v1/{path}', data=json.dumps(values).encode(),
                          headers={'Authorization': 'Bearer ' + secret})
        try:
            response = urlopen(request)
        except HTTPError as error:
            response = error
        with response:
            result = json.load(response)
        self.assertEqual(result.get('outcome'), expected, result)
        return dict(result['result'] if expected == 'done' else result)

    def project(self, name: str, *contacts: str) -> tuple[str, str]:
        page = self.notion.add_page('synthetic-projects', {**project_properties(name=name),
                    ProjectsRow.notion_name('t6_post_coordinator'): relation(*contacts)})
        registered = self.registry.register(actor='setup', idempotency_key=name, notion_page_id=page['id'])
        return str(registered.project.id), str(page['id'])  # type: ignore[union-attr]

    def mapped(self, projects: dict[str, str]) -> None:
        mapping = self.folder / 'mapping.json'
        mapping.write_text(json.dumps([{'name': name, 'project': project, 'certain': True} for name, project in projects.items()]))
        values = dict(file=str(mapping), idempotency_key='map')
        self.call('mapping apply', **values, confirm=self.call('mapping apply', **values).token)
        self.reload()

    def capture(self, text: str, posted_at: str) -> str:
        self.sequence += 1
        capture = self.http('drive/capture', 'synthetic-gateway', chat_id=-900001, message_id=self.sequence,
                            sender_telegram_id=900010, posted_at=posted_at, text=text,
                            photo=base64.b64encode(photo(str(self.folder)).data).decode())['capture']
        resolver = CaptureResolver(self.registry, self.fake, source=self.notion_source,
                                   environ={'T6_DRIVE_RECORDING_CHAT_ID': '-900001'})
        self.assertTrue(Worker(InternalQueueClient(self.queue), 'main', {'drive.capture.resolve': resolver.handle}).run_once())
        return str(capture)

    def test_project_movements_keep_coordinators_at_the_move_and_read_newest_first(self) -> None:
        old_contact, new_contact = '0b000000-0000-0000-0000-00000000c001', '0b000000-0000-0000-0000-00000000c002'
        old_person, new_person = T6Id.mint(EntityKind.PERSON), T6Id.mint(EntityKind.PERSON)
        for contact, person in ((old_contact, old_person), (new_contact, new_person)):
            self.registry.bind_entity('notion', contact, person, actor='setup')
        project, page = self.project('Synthetic Nebula', old_contact)
        other, _ = self.project('Synthetic Comet')
        self.workbook({'NEBULA': [HEADER, ['SYN D01', 'SAMPLE-01']], 'COMET': [HEADER, ['COM D01', 'SAMPLE-02']]})
        self.mapped({'NEBULA': project, 'COMET': other})
        out = self.capture('OUT SYN D01\nR: Synthetic recipient', '2026-11-24T23:00:00+08:00')
        self.notion.update_page(page, {ProjectsRow.notion_name('t6_post_coordinator'): relation(new_contact)})
        self.capture('IN SYN D01', '2026-11-24T16:00:00Z')  # Later instant, despite the earlier clock text.
        self.capture('OUT COM D01\nR: Synthetic other', '2026-11-25T09:00:00+08:00')
        result = self.http('drive/project-movements', 'synthetic-reader', project=project)
        self.assertTrue(result['practice'])
        self.assertEqual([(m['on'], m['direction'], m['label'], m['person'], m['coordinators'], m['to_review'])
                          for m in result['movements']], [
            ('2026-11-25', 'IN', 'SYN D01', 't6n_synthetickeeper', [str(new_person)], True),
            ('2026-11-24', 'OUT', 'SYN D01', 'Synthetic recipient', [str(old_person)], True)])
        drive = result['movements'][1]['drive']
        self.http('drive/approve', capture=out, item=0, choice='drive', drive=drive)
        self.notion.update_page(page, {ProjectsRow.notion_name('t6_post_coordinator'): relation()})
        rows = self.http('drive/project-movements', 'synthetic-reader', project=project)['movements']
        self.assertEqual([(m['coordinators'], m['to_review']) for m in rows], [([str(new_person)], True), ([str(old_person)], False)])

    def test_weekly_counts_and_overdue_drives_use_manila_week_and_day_boundaries(self) -> None:
        project, _ = self.project('Synthetic Nebula')
        other, _ = self.project('Synthetic Comet')
        quiet, _ = self.project('Synthetic Quiet')
        self.workbook({
            'NEBULA': [HEADER,
                ['SYN Old', 'SAMPLE-OLD', None, None, None, date(2026, 10, 1), date(2026, 11, 8), 'Synthetic holder'],
                ['SYN Fourteen', 'SAMPLE-14', None, None, None, date(2026, 10, 1), date(2026, 11, 9)],
                ['SYN Returned', 'SAMPLE-RETURN', None, None, None, date(2026, 10, 1), date(2026, 11, 1)],
                ['SYN Retired', 'SAMPLE-RETIRED', None, None, None, date(2026, 10, 1), date(2026, 11, 1)],
                ['SYN Undated', 'SAMPLE-UNDATED'], ['SYN Boundary', 'SAMPLE-BOUNDARY']],
            'NEBULA RETURN': [HEADER, ['SYN Returned', 'SAMPLE-RETURN', None, None, None, date(2026, 11, 20)]],
            'DEAD DRIVES': [HEADER, ['SYN Retired', 'SAMPLE-RETIRED']],
            'COMET': [HEADER, ['COM D01', 'SAMPLE-COM']],
            'QUIET': [HEADER, ['QUI D01', 'SAMPLE-QUI']]})
        self.mapped({'NEBULA': project, 'NEBULA RETURN': project, 'DEAD DRIVES': project, 'COMET': other, 'QUIET': quiet})
        self.capture('OUT SYN Boundary\nR: Synthetic boundary', '2026-11-22T15:59:59Z')  # Sunday in Manila.
        self.capture('IN SYN Boundary', '2026-11-22T16:00:00Z')  # Monday in Manila.
        self.capture('OUT SYN Boundary\nR: Synthetic next week', '2026-11-29T16:00:00Z')
        self.capture('OUT COM D01\nR: Synthetic other', '2026-11-25T10:00:00+08:00')
        self.now[0] = datetime.fromisoformat('2026-11-22T16:30:00Z').timestamp()
        result = self.http('drive/project-weekly', 'synthetic-reader', week='2026-11-25')
        self.assertEqual((result['week_start'], result['week_end'], result['as_of'], result['practice']),
                         ('2026-11-23', '2026-11-29', '2026-11-23', True))
        rows = {p['project']['page_id']: p for p in result['projects']}
        self.assertEqual({pid: (p['in_count'], p['out_count']) for pid, p in rows.items()},
                         {project: (1, 0), other: (0, 1), quiet: (0, 0)})
        old = next(d for d in self.http('drive/list')['drives'] if d['serial'] == 'SAMPLE-OLD')
        self.assertEqual(rows[project]['overdue'], [dict(drive_id=old['id'], label='SYN Old', serial='SAMPLE-OLD',
                         person='Synthetic holder', released_on='2026-11-08', days_out=15)])
        self.assertEqual(rows[other]['overdue'], [])
        self.assertEqual(rows[quiet]['overdue'], [])
        previous = self.http('drive/project-weekly', project=project, week='2026-11-22')
        self.assertEqual((previous['week_start'], previous['week_end']), ('2026-11-16', '2026-11-22'))
        self.assertEqual([(p['in_count'], p['out_count']) for p in previous['projects']], [(0, 1)])
        following = self.http('drive/project-weekly', project=project, week='2026-11-30')
        self.assertEqual([(p['in_count'], p['out_count']) for p in following['projects']], [(0, 1)])
        self.now[0] = datetime.fromisoformat('2026-11-23T16:00:00Z').timestamp()
        today = self.http('drive/project-weekly', project=project)
        self.assertEqual((today['week_start'], today['as_of']), ('2026-11-23', '2026-11-24'))
        self.assertEqual([(d['label'], d['days_out']) for d in today['projects'][0]['overdue']], [('SYN Old', 16), ('SYN Fourteen', 15)])

    def test_release_timestamp_uses_manila_date_for_fourteen_day_threshold(self) -> None:
        project, _ = self.project('Synthetic Nebula')
        self.workbook({'NEBULA': [HEADER, ['SYN D01', 'SAMPLE-01']]})
        self.mapped({'NEBULA': project})
        self.capture('OUT SYN D01\nR: Synthetic recipient', '2026-11-08T16:00:00Z')  # November 9 in Manila.
        self.now[0] = datetime.fromisoformat('2026-11-22T16:00:00Z').timestamp()
        result = self.http('drive/project-weekly', project=project)
        self.assertEqual(result['projects'][0]['overdue'], [])  # Exactly fourteen Manila days.
        self.now[0] = datetime.fromisoformat('2026-11-23T16:00:00Z').timestamp()
        row = self.http('drive/project-weekly', project=project)['projects'][0]['overdue'][0]
        self.assertEqual((row['released_on'], row['days_out']), ('2026-11-09', 15))

    def test_project_reads_apply_repicks_and_leave_out_cleared_skipped_and_undone_guesses(self) -> None:
        project, _ = self.project('Synthetic Nebula')
        other, _ = self.project('Synthetic Comet')
        self.workbook({'NEBULA': [HEADER, ['SYN D01', 'SAMPLE-01']], 'COMET': [HEADER, ['COM D01', 'SAMPLE-02']]})
        self.mapped({'NEBULA': project, 'COMET': other})
        out = self.capture('OUT SYN D01\nR: Synthetic recipient', '2026-11-24T10:00:00+08:00')
        changed = self.http('drive/approve', capture=out, item=0, choice='drive', typed='COM D01')
        cleared = self.capture('OUT SYN D01\nR: Synthetic recipient', '2026-11-24T11:00:00+08:00')
        self.http('drive/approve', capture=cleared, item=0, choice='none')
        skipped = self.capture('OUT SYN D01\nR: Synthetic recipient', '2026-11-24T12:00:00+08:00')
        self.http('drive/approve', capture=skipped, item=0, choice='not_a_drive')
        undone = self.capture('IN COM D01', '2026-11-25T10:00:00+08:00')
        self.http('drive/capture/undo', 'synthetic-gateway', capture=undone)
        self.assertEqual(self.http('drive/project-movements', project=project)['movements'], [])
        rows = self.http('drive/project-movements', project=other)['movements']
        self.assertEqual([(m['id'], m['label'], m['direction'], m['to_review']) for m in rows],
                         [(changed['movement'], 'COM D01', 'OUT', False)])
        weekly = self.http('drive/project-weekly', week='2026-11-25')['projects']
        self.assertEqual({p['project']['page_id']: (p['in_count'], p['out_count']) for p in weekly},
                         {project: (0, 0), other: (0, 1)})

    def test_correcting_a_movement_date_moves_its_weekly_count_and_history_date(self) -> None:
        project, _ = self.project('Synthetic Nebula')
        self.workbook({'NEBULA': [HEADER, ['SYN D01', 'SAMPLE-01']]})
        self.mapped({'NEBULA': project})
        self.capture('OUT SYN D01\nR: Synthetic recipient', '2026-11-22T23:59:00+08:00')
        (original,) = self.http('drive/project-movements', project=project)['movements']
        values = dict(movement=original['id'], on='2026-11-23', idempotency_key='synthetic-correct-date')
        preview = self.http('movement/correct', expected='preview', **values)
        corrected = self.http('movement/correct', **values, confirm=preview['token'])['movement']
        rows = self.http('drive/project-movements', project=project)['movements']
        self.assertEqual([(m['id'], m['on']) for m in rows], [(corrected['id'], '2026-11-23')])
        for week, counts in (('2026-11-22', (0, 0)), ('2026-11-23', (0, 1))):
            weekly = self.http('drive/project-weekly', project=project, week=week)['projects'][0]
            self.assertEqual((weekly['in_count'], weekly['out_count']), counts)
