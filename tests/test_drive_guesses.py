"""Plain-code Guesses and review through HTTP verbs; all evidence is synthetic."""
import base64
from datetime import date
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
from t6.llm.testing import FakeProvider
from t6.server.http import make_server
from t6.server.verbs import VerbAPI
from t6.worker import Worker
from t6.worker.internal_queue import InternalJobQueue, InternalQueueClient


class DriveGuessTests(DriveReloadFixture):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(patch.dict(os.environ, {'T6_DRIVE_RECORDING_CHAT_ID': '-900001', 'T6_DRIVE_RECORDING_TOPIC_ID': ''}))
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('t6n_synthetickeeper', 'staff', '[]', '[]', NULL, 0, 1)")
            db.execute("INSERT INTO person_permissions VALUES ('t6n_synthetickeeper', 'drives')")
        self.queue = InternalJobQueue(self.registry)
        self.fake = FakeProvider()
        api = VerbAPI(self.people, self.queue, {'synthetic-gateway': 'gateway', 'synthetic-keeper': 't6n_synthetickeeper'},
                      lambda _: None, notion_source=self.notion_source)
        self.server = make_server(('127.0.0.1', 0), api)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.sequence = 0

    def http(self, path: str, **values: Any) -> dict[str, Any]:
        request = Request(f'http://127.0.0.1:{self.server.server_port}/v1/{path}', data=json.dumps(values).encode(),
                          headers={'Authorization': 'Bearer synthetic-' + ('gateway' if path.startswith('drive/capture') else 'keeper')})
        try:
            response = urlopen(request)
        except HTTPError as error:
            response = error
        with response:
            result = json.load(response)
        self.assertEqual(result['outcome'], 'done', result)
        return dict(result['result'])

    def capture(self, text: str, *, with_notion: bool = True, **values: Any) -> dict[str, Any]:
        self.sequence += 1
        capture = self.http('drive/capture', chat_id=-900001, message_id=self.sequence, sender_telegram_id=900010,
                            posted_at='2026-11-24T10:30:00+08:00', text=text,
                            **{'photo': base64.b64encode(photo(str(self.folder)).data).decode(), **values})['capture']
        resolver = CaptureResolver(self.registry, self.fake, source=self.notion_source if with_notion else None,
                                   environ={'T6_DRIVE_RECORDING_CHAT_ID': '-900001'})
        self.assertTrue(Worker(InternalQueueClient(self.queue), 'main', {'drive.capture.resolve': resolver.handle}).run_once())
        return self.http('drive/capture/result', capture=capture)

    def inbox(self) -> list[dict[str, Any]]:
        return list(self.http('drive/inbox')['items'])

    def movements(self) -> list[dict[str, Any]]:
        return list(self.http('movement/list')['movements'])

    def inventory(self) -> dict[str, dict[str, Any]]:
        return {d['label']: d for d in self.http('drive/list')['drives']}

    def test_direction_filters_out_in_custody_for_in_and_released_for_out(self) -> None:
        self.workbook({'SYNTHETIC SHOW': [HEADER,
            ['SYN D01', 'SAMPLE-IN'],
            ['SYN D02', 'SAMPLE-OUT', None, None, None, date(2026, 10, 1), date(2026, 11, 1)],
        ]})
        self.reload()
        ids = self.inventory()
        incoming = self.capture('IN SYN')
        self.assertEqual([g['drive'] for g in self.inbox()[0]['guesses']], [ids['SYN D02']['id']])
        self.http('drive/capture/undo', capture=incoming['capture'])
        self.capture('OUT SYN\nR: Synthetic recipient')
        self.assertEqual([g['drive'] for g in self.inbox()[0]['guesses']], [ids['SYN D01']['id']])

    def test_empty_shortlist_waits_without_a_movement_and_grades_capture_thin(self) -> None:
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['SYN D01', 'SAMPLE-IN']]})
        self.reload()
        result = self.capture('OUT Unrelated zebra\nR: Synthetic recipient')
        self.assertEqual((result['grade'], result['problem']), ('thin', None))
        self.assertEqual([(i['text'], i['guesses']) for i in self.inbox()], [('Unrelated zebra', [])])
        self.assertEqual(result['items'][0]['state'], 'open')
        self.assertEqual(self.movements(), [])
        self.assertEqual(self.inventory()['SYN D01']['status'], 'in custody')

    def project(self, name: str, code: str, contact: str | None = None, active: bool = True) -> str:
        from t6.notion.generated import ProjectsRow
        from t6.notion.testing import project_properties, relation
        from t6.registry import Rotation
        page = self.notion.add_page('synthetic-projects', {**project_properties(name=name),
                    ProjectsRow.notion_name('t6_post_coordinator'): relation(*([contact] if contact else []))})
        registered = self.registry.register(actor='setup', idempotency_key=code, notion_page_id=page['id'], code=code,
                                            rotation=Rotation.IN if active else Rotation.OUT)
        return str(registered.project.id)  # type: ignore[union-attr]

    def coordinator(self) -> str:
        from t6.ids import EntityKind, T6Id
        from t6.notion.generated import ContactsRow
        from t6.notion.testing import rich_text
        self.notion.add_data_source('synthetic-contacts')
        page = self.notion.add_page('synthetic-contacts', {
            ContactsRow.notion_name('telegram_handle'): rich_text('synthetic_coordinator')})
        person = T6Id.mint(EntityKind.PERSON)
        self.registry.bind_entity('notion', page['id'], person, actor='setup')
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', 900011, 0, 1)", (str(person),))
        return str(page['id'])

    def mapped(self, projects: dict[str, str]) -> None:
        mapping = self.folder / 'mapping.json'
        mapping.write_text(json.dumps([{'name': name, 'project': project, 'certain': True} for name, project in projects.items()]))
        values = dict(file=str(mapping), idempotency_key='map')
        self.call('mapping apply', **values, confirm=self.call('mapping apply', **values).token)
        self.reload()

    def test_project_close_name_and_code_find_open_uses_without_matching_drive_labels(self) -> None:
        project = self.project('Synthetic Nebula', 'SNB')
        self.workbook({'NEBULA': [HEADER, ['Box 14', 'SAMPLE-14']], 'OTHER': [HEADER, ['SNB D01', 'SAMPLE-OTHER']]})
        self.mapped({'NEBULA': project})
        drive = self.inventory()['Box 14']['id']
        for caption in ('OUT Synthetic Nebulla D01\nR: Synthetic recipient', 'OUT SNB D01\nR: Synthetic recipient'):
            with self.subTest(caption=caption):
                result = self.capture(caption)
                self.assertEqual([g['drive'] for g in self.inbox()[0]['guesses']], [drive])
                self.http('drive/capture/undo', capture=result['capture'])

    def test_tag_of_coordinator_of_one_active_project_picks_it(self) -> None:
        contact = self.coordinator()
        picked = self.project('Synthetic Nebula', 'SNB', contact)
        inactive = self.project('Synthetic Comet', 'SCM', contact, active=False)
        self.workbook({'NEBULA': [HEADER, ['Box 14', 'SAMPLE-14']], 'COMET': [HEADER, ['Unrelated zebra', 'SAMPLE-15']]})
        self.mapped({'NEBULA': picked, 'COMET': inactive})
        self.capture('OUT Unrelated zebra @synthetic_coordinator\nR: Synthetic recipient')
        self.assertEqual([g['drive'] for g in self.inbox()[0]['guesses']], [self.inventory()['Box 14']['id']])

    def test_tag_of_coordinator_of_two_active_projects_only_breaks_a_tie(self) -> None:
        contact = self.coordinator()
        untagged = self.project('Aurora D01', 'SA1')
        tagged = self.project('Aurora D02', 'SA2', contact)
        self.project('Nebula', 'SNB', contact)  # active even without an open Drive use
        self.workbook({'ONE': [HEADER, ['Box 14', 'SAMPLE-14']], 'TWO': [HEADER, ['Box 15', 'SAMPLE-15']]})
        self.mapped({'ONE': untagged, 'TWO': tagged})
        ids = self.inventory()
        for text, expected in (('Aurora', 'Box 15'), ('Aurora D01', 'Box 14'), ('Box 14', 'Box 14')):
            result = self.capture(f'OUT {text} @synthetic_coordinator\nR: Synthetic recipient')
            self.assertEqual(self.inbox()[0]['guesses'][0]['drive'], ids[expected]['id'])
            self.http('drive/capture/undo', capture=result['capture'])
        self.capture('OUT Unrelated zebra @synthetic_coordinator\nR: Synthetic recipient')
        self.assertEqual(self.inbox()[0]['guesses'], [])

    def test_guess_one_moves_at_once_and_approval_clears_review_without_a_new_movement(self) -> None:
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['SYN D01', 'SAMPLE-01'], ['SYN D02', 'SAMPLE-02']]})
        self.reload()
        result = self.capture('OUT SYN D01\nR: Synthetic recipient')
        drive = self.inventory()['SYN D01']
        self.assertEqual(drive['status'], 'released')
        (movement,) = self.movements()
        self.assertEqual((movement['drive'], movement['to_review']), (drive['id'], True))
        (item,) = self.inbox()
        self.assertEqual(item['guesses'][0]['drive'], drive['id'])
        self.assertEqual(result['items'][0]['guesses'], item['guesses'])
        for guess in item['guesses']:
            self.assertTrue(0 < guess['confidence'] < 0.95)
            self.assertTrue(guess['reason'].strip())
            self.assertNotIn('\n', guess['reason'])
        approved = self.http('drive/approve', capture=result['capture'], item=0, choice='drive', drive=drive['id'])
        self.assertEqual((approved['state'], approved['movement']), ('approved', movement['id']))
        self.assertEqual(self.inbox(), [])
        self.assertEqual([(m['id'], m['to_review']) for m in self.movements()], [(movement['id'], False)])
        self.assertEqual(self.http('drive/approve', capture=result['capture'], item=0, choice='drive', drive=drive['id'])['movement'], movement['id'])

    def test_repicking_appends_a_correction_and_restores_the_original_drive(self) -> None:
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['SYN D01', 'SAMPLE-01'], ['SYN D02', 'SAMPLE-02']]})
        self.reload()
        ids = self.inventory()
        result = self.capture('OUT SYN D01\nR: Synthetic recipient')
        (original,) = self.movements()
        approved = self.http('drive/approve', capture=result['capture'], item=0, choice='drive', drive=ids['SYN D02']['id'])
        first, correction = self.movements()
        self.assertEqual(first['id'], original['id'])
        self.assertEqual((correction['corrects'], correction['drive'], correction['to_review']),
                         (original['id'], ids['SYN D02']['id'], False))
        self.assertEqual(approved['movement'], correction['id'])
        self.assertEqual({label: d['status'] for label, d in self.inventory().items()}, {'SYN D01': 'in custody', 'SYN D02': 'released'})
        self.http('drive/capture/undo', capture=result['capture'])
        self.assertEqual([d['status'] for d in self.inventory().values()], ['in custody', 'in custody'])

    def test_none_appends_a_clearing_correction_and_waits_without_repeating_it(self) -> None:
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['SYN D01', 'SAMPLE-01']]})
        self.reload()
        result = self.capture('OUT SYN D01\nR: Synthetic recipient')
        (original,) = self.movements()
        cleared = self.http('drive/approve', capture=result['capture'], item=0, choice='none')
        rows = self.movements()
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[1]['corrects'], rows[1]['drive'], rows[1]['to_review']), (original['id'], None, False))
        self.assertEqual((cleared['state'], cleared['drive']), ('open', None))
        self.assertEqual(self.inbox()[0]['guesses'], [])
        self.assertEqual(self.inventory()['SYN D01']['status'], 'in custody')
        self.http('drive/approve', capture=result['capture'], item=0, choice='none')
        self.assertEqual(len(self.movements()), 2)
        self.http('drive/approve', capture=result['capture'], item=0, choice='drive', drive=original['drive'])
        self.assertEqual(self.movements()[-1]['corrects'], rows[1]['id'])
        self.assertEqual(self.inventory()['SYN D01']['status'], 'released')

    def test_last_release_to_poster_or_tagged_person_ranks_up_and_guesses_stop_at_three(self) -> None:
        from t6.notion.generated import ContactsRow
        contact = self.coordinator()
        self.notion.update_page(contact, {ContactsRow.notion_name('name'): {'type': 'title', 'title': [{'text': {'content': 'Synthetic tagged'}}]}})
        with self.registry._transaction() as db:
            db.execute("UPDATE persons SET telegram_id=900010 WHERE id='t6n_synthetickeeper'")
        self.workbook({'SYNTHETIC SHOW': [HEADER, *[
            [f'SYN D0{i}', f'SAMPLE-0{i}', None, None, None, date(2026, 10, 1), date(2026, 11, 1),
             'Synthetic tagged' if i == 5 else 't6n_synthetickeeper' if i == 4 else 'Synthetic other'] for i in range(1, 8)]]})
        self.reload()
        ids = self.inventory()
        for caption, expected in (('IN SYN', ['SYN D04']), ('IN SYN @synthetic_coordinator', ['SYN D04', 'SYN D05'])):
            result = self.capture(caption)
            guesses = self.inbox()[0]['guesses']
            self.assertEqual(len(guesses), 3)
            self.assertEqual({g['drive'] for g in guesses[:len(expected)]}, {ids[label]['id'] for label in expected})
            self.http('drive/capture/undo', capture=result['capture'])

    def test_undo_after_none_closes_the_item_without_reviving_the_guess(self) -> None:
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['SYN D01', 'SAMPLE-01']]})
        self.reload()
        result = self.capture('OUT SYN D01\nR: Synthetic recipient')
        self.http('drive/approve', capture=result['capture'], item=0, choice='none')
        undone = self.http('drive/capture/undo', capture=result['capture'])
        self.assertEqual(undone['items'][0]['state'], 'closed')
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.inventory()['SYN D01']['status'], 'in custody')
        self.assertEqual(len(self.movements()), 2)

    def test_project_code_still_shortlists_when_notion_is_unavailable(self) -> None:
        project = self.project('Synthetic Nebula', 'SNB')
        self.workbook({'NEBULA': [HEADER, ['Box 14', 'SAMPLE-14']]})
        self.mapped({'NEBULA': project})
        self.capture('OUT SNB D01\nR: Synthetic recipient', with_notion=False)
        self.assertEqual([g['drive'] for g in self.inbox()[0]['guesses']], [self.inventory()['Box 14']['id']])

    def test_editing_a_resolved_problem_regrades_the_new_caption_before_resolution(self) -> None:
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['SYN D01', 'SAMPLE-01']]})
        self.reload()
        problem = self.capture('OUT SYN D01')
        self.assertEqual(problem['grade'], 'problem')
        edited = self.http('drive/capture', capture=problem['capture'], text='OUT SYN D01\nR: Synthetic recipient', edit=True)
        self.assertEqual((edited['state'], edited['grade'], edited['problem']), ('pending', 'complete', None))

    def test_not_a_drive_clears_the_guess_and_closes_the_item(self) -> None:
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['SYN D01', 'SAMPLE-01']]})
        self.reload()
        result = self.capture('OUT SYN D01\nR: Synthetic recipient')
        original = self.movements()[0]
        closed = self.http('drive/approve', capture=result['capture'], item=0, choice='not_a_drive')
        self.assertEqual((closed['state'], closed['drive']), ('not_a_drive', None))
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.movements()[1]['corrects'], original['id'])
        self.assertEqual(self.inventory()['SYN D01']['status'], 'in custody')
        self.http('drive/capture/undo', capture=result['capture'])
        self.assertEqual(len(self.movements()), 2)

    def test_a_cleared_guess_does_not_count_as_a_last_release_to_the_poster(self) -> None:
        with self.registry._transaction() as db:
            db.execute("UPDATE persons SET telegram_id=900010 WHERE id='t6n_synthetickeeper'")
        self.workbook({'SYNTHETIC SHOW': [HEADER, ['SYN D01', 'SAMPLE-01'], ['SYN D02', 'SAMPLE-02']]})
        self.reload()
        guessed = self.capture('OUT SYN D02\nR: t6n_synthetickeeper')
        self.http('drive/approve', capture=guessed['capture'], item=0, choice='none')
        self.capture('OUT SYN\nR: Synthetic recipient')
        newest = self.inbox()[0]
        self.assertEqual(newest['guesses'][0]['drive'], self.inventory()['SYN D01']['id'])
