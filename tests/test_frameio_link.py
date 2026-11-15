"""Unlinked Frame.io activity and the Link Frame.io project button (#272).

Fake gateway, fake Frame.io reader (the read API client Protocol), in-memory t6 database.
Every name below is invented.
"""
import ast
from collections.abc import Mapping
from datetime import datetime, timezone
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from t6.frameio.api import FrameioApi
from t6.frameio.client import FrameioReader, SignInNeeded
from t6.frameio.model import FrameioProject
from t6.frameio.readapi import ReadApi
from t6.frameio.reads import FrameioReads
from t6.frameio.requests import Failed, FileInfo, FrameioRequest, FrameioResult, Ok
from t6.frameio.testing import FakeFrameio, StaticTokens
from t6.frameio.worker import FrameioWorker
from t6.frameio_hold import HeldActivity
from t6.ids import EntityKind, T6Id
from t6.media import DatabaseMediaLinks, MediaStore, active_links
from t6.operator import ActionStore, OperatorService, TapResult
from t6.person import People
from t6.preview import PreviewTokens
from t6.registry import Registered, Rotation
from t6.server import Grant, JobQueue, Request, ServerConfig, WebhookApp
from t6.server.verbs import VerbAPI
from t6.verbs import Done, Preview
from t6.worker import QueueClient, Worker
from t6.worker.client import WorkerJob
from t6.worker.gateway import GatewayClient
from t6.worker.internal_queue import InternalJobQueue, InternalQueueClient
from t6.worker.operator import CardDelivery, OperatorClient
from t6.worker.review import ReviewMessage
from t6.worker.review_hold import HoldUnlinked
from t6.worker.review_target import ReviewTarget
from test_operator import CALLBACK_PREFIX, GATEWAY_SECRET, FakeGateway
from test_person import MemoryRegistry
from test_server_app import TAP_SECRET, InProcess

OPERATOR = 42
STAFF = 43
CLIENT = 44
REVIEW_CHAT = -100500
WORKER = 'worker-secret'
MESSAGE = 777


def person_id(letter: str) -> str:
    return 't6n_' + letter * 32


def when(day: int) -> datetime:
    return datetime(2026, 11, day, tzinfo=timezone.utc)


class FakeProjects:
    """The read API client's `projects()`: what the Frame.io worker lists for the configured account."""

    def __init__(self) -> None:
        self.items: list[FrameioProject] = []
        self.failure: Failed | None = None
        self.calls = 0

    def add(self, project_id: str, name: str, day: int) -> None:
        self.items.append(FrameioProject(id=project_id, name=name, updated_at=when(day)))

    def projects(self) -> tuple[FrameioProject, ...] | Failed:
        self.calls += 1
        return self.failure if self.failure is not None else tuple(self.items)


class LinkCase(unittest.TestCase):
    def waiting_rows(self, frameio_project_id: str | None = None) -> list[dict[str, Any]]:
        """The held rows not released yet, oldest first (read from the table: the production class has no read for it)."""
        with self.registry._transaction() as db:
            rows = db.execute('SELECT id, frameio_project_id, dedupe_key, payload FROM frameio_held_activity WHERE released_at IS NULL AND (? IS NULL OR frameio_project_id = ?) ORDER BY id',
                              (frameio_project_id, frameio_project_id)).fetchall()
        return [{'id': r[0], 'frameio_project_id': r[1], 'dedupe_key': r[2], 'payload': json.loads(r[3])} for r in rows]

    def setUp(self) -> None:
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.people = People(self.registry, PreviewTokens('test'))
        self.operator_person, self.staff_person, self.client_person = person_id('a'), person_id('b'), person_id('c')
        with self.registry._transaction() as db:
            for who, kind, telegram in ((self.operator_person, 'staff', OPERATOR), (self.staff_person, 'staff', STAFF), (self.client_person, 'client', CLIENT)):
                db.execute("INSERT INTO persons VALUES (?, ?, '[]', '[]', ?, 0, 1)", (who, kind, telegram))
            db.execute("INSERT INTO person_permissions VALUES (?, 'operator')", (self.operator_person,))
        self.show_a, self.show_b = self.show('SHOWA'), self.show('SHOWB')
        self.reader = FakeProjects()
        self.reader.add('fp-old', 'Oldest cut', 1)
        self.reader.add('fp-new', 'Newest cut', 9)
        self.reader.add('fp-mid', 'Middle cut', 5)
        self.actions = ActionStore(self.tmp.name + '/actions')
        self.service = OperatorService(self.registry, self.actions, (OPERATOR,), people=self.people, frameio=self.reader)
        self.keys = 0

    def show(self, code: str) -> str:
        made = self.registry.register(actor='setup', idempotency_key='reg-' + code, code=code, rotation=Rotation.IN)
        assert isinstance(made, Registered)
        return made.project.id

    def link(self, project: str, frameio_project: str) -> None:
        store = MediaStore(self.registry, PreviewTokens('test'))
        values = {'project': project, 'frameio_project': frameio_project}
        key = f'link-{project}-{frameio_project}'
        preview = store.frameio_project_write('link', values, actor=self.operator_person, idempotency_key=key)
        assert isinstance(preview, Preview)
        assert isinstance(store.frameio_project_write('link', values, actor=self.operator_person, idempotency_key=key, confirm=preview.token), Done)

    def unlink(self, project: str, frameio_project: str) -> None:
        store = MediaStore(self.registry, PreviewTokens('test'))
        values = {'project': project, 'frameio_project': frameio_project}
        preview = store.frameio_project_write('unlink', values, actor=self.operator_person, idempotency_key='unlink-' + frameio_project)
        assert isinstance(preview, Preview)
        assert isinstance(store.frameio_project_write('unlink', values, actor=self.operator_person, idempotency_key='unlink-' + frameio_project, confirm=preview.token), Done)

    def linked(self) -> dict[str, str]:
        return {link.ref: link.project_id for link in active_links(self.registry, 'frameio-project')}

    # -- the service, driven the way the gateway drives it ----------------------------------

    def next_key(self) -> str:
        self.keys += 1
        return f'key-{self.keys}'

    def open_card(self, who: int = OPERATOR) -> str:
        result = self.service.command('', operator=who, chat=who, key=self.next_key())
        self.assertEqual(result.outcome, 'opened')
        assert result.action is not None
        self.service.delivered(result.action.token, 1, MESSAGE)
        return result.action.token

    def texts(self, token: str) -> list[str]:
        card = self.service.card(token)
        assert card is not None
        return [button.text for row in card.buttons for button in row]

    def press(self, token: str, text: str, who: int = OPERATOR) -> TapResult:
        card = self.service.card(token)
        assert card is not None
        for row in card.buttons:
            for button in row:
                if button.text.startswith(text):
                    return self.service.tap(button.payload, operator=who, chat=card.chat, message_id=MESSAGE, tap_key=self.next_key())
        raise AssertionError(f'no button {text!r} in {self.texts(token)}')

    def body(self, token: str) -> str:
        card = self.service.card(token)
        assert card is not None
        return card.text

    def open_frameio_list(self, show: str = 'SHOWA', who: int = OPERATOR) -> str:
        token = self.open_card(who)
        self.assertEqual(self.press(token, show, who).outcome, 'project')
        self.assertEqual(self.press(token, 'Link Frame.io project', who).outcome, 'frameio_list')
        return token

    def frameio_buttons(self, token: str) -> list[str]:
        fixed = {'Search', 'Clear search', 'Next', 'Previous', 'Back', 'Close', 'Cancel'}
        return [text for text in self.texts(token) if text not in fixed]


class LinkButtonTests(LinkCase):
    def test_the_project_card_offers_the_button_only_when_frameio_can_be_read(self) -> None:
        token = self.open_card()
        self.press(token, 'SHOWA')
        self.assertIn('Link Frame.io project', self.texts(token))
        bare = OperatorService(self.registry, ActionStore(self.tmp.name + '/bare'), (OPERATOR,), people=self.people)
        opened = bare.command('', operator=OPERATOR, chat=OPERATOR, key='bare')
        assert opened.action is not None
        bare.delivered(opened.action.token, 1, MESSAGE)
        picked = bare.card(opened.action.token)
        assert picked is not None
        bare.tap(picked.buttons[0][0].payload, operator=OPERATOR, chat=OPERATOR, message_id=MESSAGE, tap_key='bare-tap')
        again = bare.card(opened.action.token)
        assert again is not None
        self.assertNotIn('Link Frame.io project', [b.text for row in again.buttons for b in row])

    def test_the_list_is_newest_first(self) -> None:
        token = self.open_frameio_list()
        self.assertEqual(self.frameio_buttons(token), ['Newest cut', 'Middle cut', 'Oldest cut'])

    def test_every_project_linked_to_any_show_is_hidden(self) -> None:
        self.link(self.show_b, 'fp-new')
        token = self.open_frameio_list()
        self.assertEqual(self.frameio_buttons(token), ['Middle cut', 'Oldest cut'])
        self.assertNotIn('Newest cut', self.body(token))

    def test_an_unlinked_project_comes_back_to_the_list(self) -> None:
        self.link(self.show_b, 'fp-new')
        self.unlink(self.show_b, 'fp-new')
        self.assertIn('Newest cut', self.frameio_buttons(self.open_frameio_list()))

    def test_search_narrows_the_list_and_clear_search_brings_it_back(self) -> None:
        token = self.open_frameio_list()
        self.assertEqual(self.press(token, 'Search').outcome, 'frameio_search')
        typed = self.service.command('MID', operator=OPERATOR, chat=OPERATOR, key=self.next_key())
        self.assertEqual(typed.outcome, 'frameio_list')
        self.assertEqual(self.frameio_buttons(token), ['Middle cut'])
        self.assertIn('MID', self.body(token))
        self.press(token, 'Clear search')
        self.assertEqual(self.frameio_buttons(token), ['Newest cut', 'Middle cut', 'Oldest cut'])

    def test_a_search_with_no_match_says_so(self) -> None:
        token = self.open_frameio_list()
        self.press(token, 'Search')
        self.service.command('zzz', operator=OPERATOR, chat=OPERATOR, key=self.next_key())
        self.assertEqual(self.frameio_buttons(token), [])
        self.assertIn('No unlinked Frame.io project', self.body(token))

    def test_a_long_list_pages_five_at_a_time(self) -> None:
        for number in range(10, 17):
            self.reader.add(f'fp-{number}', f'Extra {number}', number)
        token = self.open_frameio_list()
        first = self.frameio_buttons(token)
        self.assertEqual(len(first), 5)
        self.press(token, 'Next')
        second = self.frameio_buttons(token)
        self.assertEqual(len(second), 5)
        self.assertFalse(set(first) & set(second))

    def test_choosing_a_project_links_it_to_the_show_as_the_tapper(self) -> None:
        token = self.open_frameio_list()
        result = self.press(token, 'Middle cut')
        self.assertEqual(result.outcome, 'frameio_linked')
        self.assertEqual(self.linked(), {'fp-mid': self.show_a})
        with self.registry._transaction() as db:
            self.assertEqual(db.execute('SELECT actor FROM media_history').fetchall(), [(self.operator_person,)])
        self.assertIn('Middle cut', self.body(token))
        self.assertIn('SHOWA', self.body(token))
        again = self.service.tap(f'op:{token}:p0', operator=OPERATOR, chat=OPERATOR, message_id=MESSAGE, tap_key=self.next_key())
        self.assertEqual(again.outcome, 'already_handled')

    def test_a_second_show_choosing_a_linked_project_is_refused_naming_the_holder(self) -> None:
        token = self.open_frameio_list('SHOWB')
        self.assertIn('Newest cut', self.frameio_buttons(token))
        self.link(self.show_a, 'fp-new')  # another person linked it after this list was shown
        result = self.press(token, 'Newest cut')
        self.assertEqual(result.outcome, 'frameio_held')
        self.assertEqual(self.linked(), {'fp-new': self.show_a})
        self.assertIn('SHOWA', self.body(token))
        self.assertIn(self.show_a, self.body(token))

    def test_a_reader_failure_is_said_on_the_card_and_changes_nothing(self) -> None:
        self.reader.failure = SignInNeeded()
        token = self.open_frameio_list()
        self.assertEqual(self.frameio_buttons(token), [])
        self.assertIn('sign in', self.body(token).lower())
        self.assertEqual(self.linked(), {})
        self.reader.failure = Failed('Frame.io read API answered 502: boom')
        self.assertEqual(self.press(token, 'Back').outcome, 'project')
        self.press(token, 'Link Frame.io project')
        self.assertIn('did not answer', self.body(token))

    def test_names_are_escaped_on_the_card(self) -> None:
        self.reader.add('fp-x', 'Show <One> & Co', 12)
        token = self.open_frameio_list()
        self.assertIn('Show &lt;One&gt; &amp; Co', self.body(token))

    def test_buttons_fit_telegrams_limit(self) -> None:
        token = self.open_frameio_list()
        card = self.service.card(token)
        assert card is not None
        for row in card.buttons:
            for button in row:
                self.assertLessEqual(len((CALLBACK_PREFIX + button.payload).encode()), 64)


class StaffButtonTests(LinkCase):
    def test_any_active_staff_person_opens_the_card_and_links_with_only_the_link_button(self) -> None:
        token = self.open_card(STAFF)
        self.assertEqual(self.press(token, 'SHOWA', STAFF).outcome, 'project')
        self.assertEqual(self.texts(token), ['Link Frame.io project', 'Close'])
        self.assertEqual(self.press(token, 'Link Frame.io project', STAFF).outcome, 'frameio_list')
        self.assertEqual(self.press(token, 'Oldest cut', STAFF).outcome, 'frameio_linked')
        self.assertEqual(self.linked(), {'fp-old': self.show_a})
        with self.registry._transaction() as db:
            self.assertEqual(db.execute('SELECT actor FROM media_history').fetchall(), [(self.staff_person,)])

    def test_a_staff_card_cannot_change_a_code_or_rotation(self) -> None:
        token = self.open_card(STAFF)
        self.press(token, 'SHOWA', STAFF)
        before = self.registry.get(self.show_a)
        for choice in ('out', 'in', 'clr', 'chg', 'cclr', 'bout'):
            result = self.service.tap(f'op:{token}:{choice}', operator=STAFF, chat=STAFF, message_id=MESSAGE, tap_key=self.next_key())
            self.assertEqual(result.outcome, 'rejected', choice)
        self.assertEqual(self.registry.get(self.show_a), before)

    def test_a_client_cannot_open_the_card_and_an_operator_card_is_not_a_staff_card(self) -> None:
        self.assertEqual(self.service.command('', operator=CLIENT, chat=CLIENT, key='client').outcome, 'rejected')
        token = self.open_card()
        refused = self.service.tap(f'op:{token}:p0', operator=STAFF, chat=OPERATOR, message_id=MESSAGE, tap_key='staff-on-operator-card')
        self.assertEqual(refused.outcome, 'rejected')

    def test_staff_cannot_open_it_when_frameio_cannot_be_read(self) -> None:
        bare = OperatorService(self.registry, ActionStore(self.tmp.name + '/bare'), (OPERATOR,), people=self.people)
        self.assertEqual(bare.command('', operator=STAFF, chat=STAFF, key='bare').outcome, 'rejected')

    def test_an_inactive_staff_person_cannot_open_it(self) -> None:
        with self.registry._transaction() as db:
            db.execute('UPDATE persons SET active = 0 WHERE id = ?', (self.staff_person,))
        self.assertEqual(self.service.command('', operator=STAFF, chat=STAFF, key='inactive').outcome, 'rejected')


class MainWorkerCase(LinkCase):
    """The API and the main worker, joined by the internal job table, with a fake gateway."""

    def setUp(self) -> None:
        super().setUp()
        self.queue = JobQueue(self.tmp.name + '/queue')
        self.inside = InternalJobQueue(self.registry)
        config = ServerConfig(tap_secret=TAP_SECRET, operator_ids=(OPERATOR,), grants=(
            Grant(WORKER, operator=True, registry_read=True, claim=frozenset({'main'}), submit=frozenset({'main'}), actor='main-worker'),))
        self.app = WebhookApp(self.queue, config, registry=self.registry, operator=self.service, inside=self.inside)
        self.gateway = FakeGateway()
        transport = InProcess(self.app)
        target = ReviewTarget(DatabaseMediaLinks(self.registry), self.registry)
        self.held = HeldActivity(self.registry)
        offer = OperatorClient(transport, WORKER).offer_frameio_link
        review = ReviewMessage(GatewayClient(self.gateway, GATEWAY_SECRET), REVIEW_CHAT, None, self.ask, target=target,
                               is_linked=target.linked, hold=HoldUnlinked(self.held, offer))
        handlers = {**CardDelivery(OperatorClient(transport, WORKER), GatewayClient(self.gateway, GATEWAY_SECRET)).handlers(), **review.handlers()}
        self.worker = Worker(QueueClient(transport, WORKER), 'main', handlers, internal_client=InternalQueueClient(self.inside))
        self.updates = 0

    def ask(self, request: FrameioRequest) -> FrameioResult:
        if isinstance(request, FileInfo):
            return Ok({'id': request.file_id, 'name': 'Cut 1', 'url': 'https://frame.example/f', 'parent_id': None})
        return Ok([])

    def drain(self) -> None:
        while self.worker.run_once():
            pass

    def event(self, frameio_project: str, key: str) -> None:
        self.inside.submit('main', 'frameio.review_done', key, {
            'file_id': 'file-' + key, 'frameio_project_id': frameio_project, 'user_id': 'u1', 'user_name': 'Ana Reyes',
            'count': 2, 'first_at': 1_790_000_000.0, 'last_at': 1_790_000_060.0})

    def cards(self) -> dict[int, dict[str, Any]]:
        return {ref: message for ref, message in self.gateway.messages.items() if message['buttons']}

    def reviews(self) -> list[dict[str, Any]]:
        return [message for message in self.gateway.messages.values() if 'finished commenting' in message['text']]

    def tap(self, payload: str, who: int = OPERATOR, chat: int = OPERATOR, ref: int = 1) -> dict[str, Any]:
        self.updates += 1
        body = {'kind': 'tap', 'data': payload, 'from': {'id': who}, 'chat': chat, 'message_id': 1000 + ref, 'update_id': self.updates}
        headers = {'Authorization': f'Bearer {TAP_SECRET}', 'Idempotency-Key': f'tap-{self.updates}'}
        answer = self.app.tailnet(Request('POST', '/tap', headers, json.dumps(body).encode()))
        self.assertEqual(answer.status, 200, answer.body)
        return dict(answer.body)

    def button(self, ref: int, text: str) -> str:
        for row in self.gateway.messages[ref]['buttons']:
            for button in row:
                if button['text'].startswith(text):
                    return CALLBACK_PREFIX + str(button['payload'])
        raise AssertionError(f'no button {text!r} on {self.gateway.messages[ref]["buttons"]}')

    def pick(self, ref: int, show: str = 'SHOWA', who: int = OPERATOR) -> dict[str, Any]:
        return self.tap(self.button(ref, show)[len(CALLBACK_PREFIX):], who=who, ref=ref)

    def only_card(self) -> int:
        cards = self.cards()
        self.assertEqual(len(cards), 1)
        return int(next(iter(cards)))


class UnlinkedActivityTests(MainWorkerCase):
    def test_events_for_an_unlinked_project_are_held_and_one_card_is_posted_per_project(self) -> None:
        for number in (1, 2, 3):
            self.event('fp-1', f'evt-{number}')
        self.event('fp-2', 'evt-4')
        self.drain()
        self.assertEqual(self.reviews(), [])
        self.assertEqual([len(self.waiting_rows(project)) for project in ('fp-1', 'fp-2')], [3, 1])
        self.assertEqual(len(self.cards()), 2)
        self.assertEqual(self.gateway.sends, 2)

    def test_the_same_event_twice_is_held_once(self) -> None:
        self.event('fp-1', 'evt-1')
        self.drain()
        self.assertFalse(self.held.hold('fp-1', 'evt-1', {'file_id': 'file-evt-1'}))
        self.assertEqual(len(self.waiting_rows('fp-1')), 1)

    def test_the_card_names_the_frameio_project_offers_shows_and_is_for_coordinators(self) -> None:
        self.event('fp-new', 'evt-1')
        self.drain()
        card = self.gateway.messages[self.only_card()]
        self.assertEqual(card['chat'], OPERATOR)
        self.assertIn('Newest cut', card['text'])
        shows = {button['text'].split(' ')[0] for row in card['buttons'] for button in row}
        self.assertTrue({'SHOWA', 'SHOWB'} <= shows)
        for row in card['buttons']:
            for button in row:
                self.assertLessEqual(len((CALLBACK_PREFIX + button['payload']).encode()), 64)
        stored = self.service.store.find_offer('frameio_link:fp-new')
        assert stored is not None
        self.assertEqual(stored.allowed_tapper, 'permission:coordinator')

    def test_choosing_a_show_links_it_and_releases_the_held_activity_once(self) -> None:
        for number in (1, 2, 3):
            self.event('fp-1', f'evt-{number}')
        self.drain()
        ref = self.only_card()
        self.assertEqual(self.pick(ref)['outcome'], 'frameio_linked')
        self.assertEqual(self.linked(), {'fp-1': self.show_a})
        self.drain()
        self.assertEqual(len(self.reviews()), 3)
        self.assertEqual(self.waiting_rows('fp-1'), [])
        self.assertIn('Linked', self.gateway.messages[ref]['text'])
        token = self.service.store.find_offer('frameio_link:fp-1')
        assert token is not None
        self.assertEqual(self.tap(f'op:{token.token}:p0', ref=ref)['outcome'], 'already_handled')
        self.assertEqual(self.app.release_held_activity(), 0)
        self.drain()
        self.assertEqual(len(self.reviews()), 3)

    def test_an_event_after_the_link_is_sent_at_once_not_held(self) -> None:
        self.link(self.show_a, 'fp-1')
        self.event('fp-1', 'evt-1')
        self.drain()
        self.assertEqual((len(self.reviews()), self.cards(), self.waiting_rows()), (1, {}, []))

    def test_a_person_who_is_not_a_coordinator_cannot_choose(self) -> None:
        self.event('fp-1', 'evt-1')
        self.drain()
        ref = self.only_card()
        self.assertEqual(self.pick(ref, who=STAFF)['outcome'], 'rejected')
        self.assertEqual(self.pick(ref, who=CLIENT)['outcome'], 'rejected')
        self.assertEqual((self.linked(), len(self.waiting_rows('fp-1'))), ({}, 1))

    def test_a_client_with_a_coordinator_permission_cannot_link(self) -> None:
        with self.registry._transaction() as db:
            db.execute("INSERT INTO person_permissions VALUES (?, 'coordinator')", (self.client_person,))
        self.event('fp-1', 'evt-1')
        self.drain()
        ref = self.only_card()
        self.assertEqual(self.pick(ref, who=CLIENT)['outcome'], 'rejected')
        self.assertEqual(self.linked(), {})

    def test_a_link_made_by_the_verb_releases_the_held_activity_and_the_old_card_is_refused(self) -> None:
        self.event('fp-1', 'evt-1')
        self.drain()
        ref = self.only_card()
        self.link(self.show_b, 'fp-1')  # someone ran `frameio project link` meanwhile
        self.assertEqual(self.app.release_held_activity(), 1)
        self.drain()
        self.assertEqual(len(self.reviews()), 1)
        self.assertEqual(self.pick(ref)['outcome'], 'frameio_held')
        self.drain()
        self.assertEqual(self.linked(), {'fp-1': self.show_b})
        self.assertIn('SHOWB', self.gateway.messages[ref]['text'])
        self.assertEqual(len(self.reviews()), 1)

    def test_the_frameio_project_verb_releases_held_activity(self) -> None:
        self.event('fp-1', 'evt-1')
        self.drain()
        api = VerbAPI(self.people, self.queue, {'person': self.operator_person}, lambda _: None, self.app)
        headers = {'Authorization': 'Bearer person'}
        with patch.dict('os.environ', {'T6_PREVIEW_SIGNING_KEY': 'test'}):
            values = {'project': self.show_a, 'frameio_project': 'fp-1', 'idempotency_key': 'verb-link'}
            preview = api(Request('POST', '/v1/frameio/project/link', headers, json.dumps(values).encode()))
            self.assertEqual(preview.body['outcome'], 'preview')
            done = api(Request('POST', '/v1/frameio/project/link', headers, json.dumps({**values, 'confirm': preview.body['token']}).encode()))
            self.assertEqual(done.body['outcome'], 'done')
        self.drain()
        self.assertEqual(len(self.reviews()), 1)
        self.assertEqual(self.waiting_rows('fp-1'), [])

    def verb(self, path: str, values: dict[str, Any]) -> dict[str, Any]:
        api = VerbAPI(self.people, self.queue, {'person': self.operator_person}, lambda _: None, self.app)
        with patch.dict('os.environ', {'T6_PREVIEW_SIGNING_KEY': 'test'}):
            preview = api(Request('POST', path, {'Authorization': 'Bearer person'}, json.dumps(values).encode()))
            self.assertEqual(preview.body['outcome'], 'preview', preview.body)
            done = api(Request('POST', path, {'Authorization': 'Bearer person'}, json.dumps({**values, 'confirm': preview.body['token']}).encode()))
        self.assertEqual(done.body['outcome'], 'done', done.body)
        return dict(done.body)

    def test_media_link_of_a_frameio_project_releases_the_held_activity(self) -> None:
        self.event('fp-1', 'evt-1')
        self.drain()
        self.verb('/v1/media/link', {'owner': self.show_a, 'kind': 'frameio-project', 'ref': 'fp-1', 'role': 'frameio-project', 'idempotency_key': 'media-link'})
        self.drain()
        self.assertEqual((len(self.reviews()), self.waiting_rows('fp-1')), (1, []))

    def test_after_verb_does_nothing_after_a_read_verb_or_a_refusal(self) -> None:
        from t6.project_writes import ProjectWrites
        from t6.verbs import Refused
        self.event('fp-1', 'evt-1')
        self.drain()
        self.link(self.show_a, 'fp-1')  # the activity waits for something to release it
        writes = ProjectWrites(self.registry, self.people.tokens)
        archive = writes.run('archive', self.show_b, None, actor=self.operator_person, idempotency_key='archive')
        assert isinstance(archive, Preview)
        writes.run('archive', self.show_b, None, actor=self.operator_person, idempotency_key='archive', confirm=archive.token)
        self.assertEqual(len(self.registry.undispatched_status_sync()), 1)  # committed, not queued yet
        self.app.after_verb('project', 'list', Done(result={}))
        self.app.after_verb('project', 'archive', Refused(rule='x', reason='y'))
        self.assertEqual((len(self.waiting_rows('fp-1')), len(self.registry.undispatched_status_sync())), (1, 1))
        self.app.after_verb('project', 'archive', Done(result={}))
        self.assertEqual((len(self.waiting_rows('fp-1')), len(self.registry.undispatched_status_sync())), (0, 0))

    def test_after_verb_does_nothing_after_concern_list_concern_changes_or_project_notes(self) -> None:
        self.event('fp-1', 'evt-1')
        self.drain()
        self.link(self.show_a, 'fp-1')  # the activity waits for something to release it
        for noun, verb in (('concern', 'list'), ('concern', 'changes'), ('project', 'notes')):
            with self.subTest(noun=noun, verb=verb):
                self.app.after_verb(noun, verb, Done(result={}))
                self.assertEqual(len(self.waiting_rows('fp-1')), 1)

    def test_an_error_in_after_verb_is_logged_and_the_committed_verb_still_answers_done(self) -> None:
        def failing() -> int:
            raise RuntimeError('the hook broke')

        with patch.object(self.app, 'release_held_activity', failing), self.assertLogs('t6.server.app', 'ERROR') as logged:
            done = self.verb('/v1/media/link', {'owner': self.show_a, 'kind': 'frameio-project', 'ref': 'fp-1', 'role': 'frameio-project',
                                                'idempotency_key': 'media-link'})
        self.assertEqual(done['outcome'], 'done')
        self.assertIn('the hook broke', '\n'.join(logged.output))
        self.assertEqual(len(active_links(self.registry, 'frameio-project')), 1)

    def test_the_admin_media_migration_route_releases_the_held_activity_like_the_other_link_routes(self) -> None:
        """The route sits outside the verb registry; a link made by any other way is released when the migration is confirmed."""
        from t6.notion.testing import FakeNotion, external_link_properties
        self.event('fp-1', 'evt-1')
        self.drain()
        self.link(self.show_a, 'fp-1')  # the link is there, the activity is still held: nothing has released it yet
        self.assertEqual(len(self.waiting_rows('fp-1')), 1)
        fake = FakeNotion()
        fake.add_data_source('links')
        fake.add_page('links', external_link_properties(system='resolve', kind='project', external_id='db/show', projects=['project-page'], active=True))
        with self.registry._transaction() as db:
            db.execute("INSERT INTO person_permissions VALUES (?, 'admin')", (self.operator_person,))
        api = VerbAPI(self.people, self.queue, {'person': self.operator_person}, lambda _: fake.client(), self.app)
        api.data_sources['external_links'] = 'links'
        headers = {'Authorization': 'Bearer person'}
        with patch.dict('os.environ', {'T6_PREVIEW_SIGNING_KEY': 'test', 'T6_NOTION_TOKEN': 'fake'}):
            preview = api(Request('POST', '/v1/admin/media/migrate', headers, json.dumps({'idempotency_key': 'migrate'}).encode()))
            self.assertEqual(preview.body['outcome'], 'preview', preview.body)
            done = api(Request('POST', '/v1/admin/media/migrate', headers, json.dumps({'idempotency_key': 'migrate', 'confirm': preview.body['token']}).encode()))
        self.assertEqual(done.body['outcome'], 'done', done.body)
        self.drain()
        self.assertEqual((len(self.reviews()), self.waiting_rows('fp-1')), (1, []))

    def test_project_start_through_the_api_releases_the_held_activity(self) -> None:
        """A real /v1/project/start preview, job and confirm: the Frame.io step links the project, and the held review is sent once."""
        from t6.frameio.project_folders import ProjectFolders
        from t6.project_start import ProjectStartStore

        class Writer:
            def create_project_folders(self, project_id: str, name: str, folders: tuple[str, ...], *,
                                       known_project_id: str | None = None, taken: tuple[str, ...] = ()) -> ProjectFolders:
                return ProjectFolders('fp-1', {code: 'folder-' + code for code in folders}, True, folders)

        made = self.registry.register(actor='setup', idempotency_key='reg-start', code='START', rotation=Rotation.IN, notion_page_id='start-page')
        assert isinstance(made, Registered)
        project = made.project.id
        self.registry.review_chat_links = lambda page: []  # type: ignore[attr-defined]  # the memory registry has no chat links; the review goes to the default chat
        self.event('fp-1', 'evt-1')
        self.drain()
        self.assertEqual((len(self.reviews()), bool(self.waiting_rows('fp-1'))), (0, True))
        api = VerbAPI(self.people, self.queue, {'person': self.operator_person}, lambda _: None, self.app)
        api.frameio = Writer()  # type: ignore[assignment]  # the Frame.io worker's answer; the queue to it is not what is tested here
        store = ProjectStartStore(self.registry, self.people.tokens, lambda page: 'Feature Film', lambda page: 'Start Show')
        planner = Worker(InternalQueueClient(self.inside), 'main', {'project_start.preview': lambda job: store.run_preview(job.payload)})
        headers = {'Authorization': 'Bearer person'}

        def post(values: dict[str, Any]) -> dict[str, Any]:
            return dict(api(Request('POST', '/v1/project/start', headers, json.dumps(values).encode())).body)

        values = {'project': project, 'deliverables': [{'code': 'Ep01', 'episode': 'Episode 1', 'due': '2026-12-01', 'air': None}],
                  'media': [], 'bookings': []}
        with patch.dict('os.environ', {'T6_PREVIEW_SIGNING_KEY': 'test'}):
            started = post({**values, 'idempotency_key': 'start-e2e'})
            self.assertEqual(started['outcome'], 'done', started)
            while planner.run_once():
                pass
            preview = post({'project': project, 'job': started['result']['job_id']})
            self.assertEqual(preview['outcome'], 'preview', preview)
            done = post({**values, 'idempotency_key': 'start-e2e', 'confirm': preview['token']})
        self.assertEqual(done['outcome'], 'done', done)
        self.drain()
        self.assertEqual((len(self.reviews()), self.waiting_rows('fp-1')), (1, []))

    def test_any_done_verb_releases_activity_whose_project_a_link_made_outside_the_verb_path(self) -> None:
        """The decision is the link data, not a verb name: `project start` links through the Media store inside its own steps."""
        from t6.verbs import Done as Finished
        self.event('fp-1', 'evt-1')
        self.drain()
        self.link(self.show_a, 'fp-1')  # what the Frame.io step of `project start` does inside its confirm
        self.app.after_verb('project', 'start', Finished(result={}))
        self.drain()
        self.assertEqual((len(self.reviews()), self.waiting_rows('fp-1')), (1, []))

    def test_a_retried_job_whose_event_is_held_or_released_sends_no_second_message(self) -> None:
        self.event('fp-1', 'evt-1')
        self.drain()  # held
        self.link(self.show_a, 'fp-1')
        self.assertEqual(self.app.release_held_activity(), 1)
        self.drain()
        self.assertEqual(len(self.reviews()), 1)
        job = WorkerJob(99, 'main', 'frameio.review_done', 'evt-1', {
            'file_id': 'file-evt-1', 'frameio_project_id': 'fp-1', 'user_id': 'u1', 'user_name': 'Ana Reyes',
            'count': 2, 'first_at': 1_790_000_000.0, 'last_at': 1_790_000_060.0}, 't', 2)
        review = ReviewMessage(GatewayClient(self.gateway, GATEWAY_SECRET), REVIEW_CHAT, None, self.ask, target=ReviewTarget(DatabaseMediaLinks(self.registry), self.registry),
                               is_linked=lambda project: True, hold=HoldUnlinked(self.held, lambda project: None), is_held=self.held.has)
        review.notify(job)  # the retry of the job that was held
        self.assertEqual(len(self.reviews()), 1)

    def test_a_failed_card_offer_then_a_link_sends_the_event_once(self) -> None:
        offers = {'fail': True}

        def offer(project: str) -> None:
            if offers['fail']:
                raise RuntimeError('API did not answer')

        target = ReviewTarget(DatabaseMediaLinks(self.registry), self.registry)
        review = ReviewMessage(GatewayClient(self.gateway, GATEWAY_SECRET), REVIEW_CHAT, None, self.ask, target=target,
                               is_linked=target.linked, hold=HoldUnlinked(self.held, offer), is_held=self.held.has)
        self.worker = Worker(QueueClient(InProcess(self.app), WORKER), 'main', review.handlers(), internal_client=InternalQueueClient(self.inside))
        self.event('fp-1', 'evt-1')
        self.drain()  # held, the offer failed, the job is waiting for its retry
        self.assertEqual(len(self.waiting_rows('fp-1')), 1)
        self.link(self.show_a, 'fp-1')
        self.assertEqual(self.app.release_held_activity(), 1)
        offers['fail'] = False
        import time
        self.inside._clock = lambda: time.time() + 3600  # the retry is due now
        for _ in range(5):
            self.drain()
        self.assertEqual(len(self.reviews()), 1)

    def test_a_restart_with_held_activity_for_a_linked_project_releases_it(self) -> None:
        self.event('fp-1', 'evt-1')
        self.drain()
        self.link(self.show_a, 'fp-1')
        WebhookApp(self.queue, ServerConfig(tap_secret=TAP_SECRET, operator_ids=(OPERATOR,)), registry=self.registry, operator=self.service, inside=self.inside)
        self.drain()
        self.assertEqual(len(self.reviews()), 1)

    def test_held_activity_is_not_dropped_when_the_card_is_skipped_and_a_new_event_posts_a_new_card(self) -> None:
        self.event('fp-1', 'evt-1')
        self.drain()
        ref = self.only_card()
        self.assertEqual(self.tap(self.button(ref, 'Not now')[len(CALLBACK_PREFIX):], ref=ref)['outcome'], 'skipped')
        self.drain()
        self.assertEqual(len(self.waiting_rows('fp-1')), 1)
        self.event('fp-1', 'evt-2')
        self.drain()
        self.assertEqual(len(self.waiting_rows('fp-1')), 2)
        live = [card for card in self.cards().values() if card['text'].startswith('<b>Frame.io') and 'Not now' in json.dumps(card['buttons'])]
        self.assertEqual(len(live), 1)

    def test_no_message_text_is_stored_with_held_activity(self) -> None:
        self.event('fp-1', 'evt-1')
        self.drain()
        (row,) = self.waiting_rows('fp-1')
        self.assertEqual(set(row['payload']) - {'file_id', 'frameio_project_id', 'user_id', 'user_name', 'count', 'first_at', 'last_at'}, set())


class HeldActivityStoreTests(LinkCase):
    def test_held_activity_has_no_read_of_waiting_rows_that_nothing_in_production_uses(self) -> None:
        self.assertFalse(hasattr(HeldActivity, 'waiting'))

    def test_hold_is_idempotent_and_release_sends_each_row_once(self) -> None:
        held = HeldActivity(self.registry)
        self.assertTrue(held.hold('fp-1', 'k1', {'count': 1}))
        self.assertFalse(held.hold('fp-1', 'k1', {'count': 1}))
        held.hold('fp-2', 'k2', {'count': 2})
        sent: list[tuple[str, Mapping[str, Any]]] = []

        def submit(queue: str, kind: str, key: str, payload: Mapping[str, Any]) -> object:
            sent.append((queue, payload))
            return None

        self.assertEqual(held.release_linked(submit), 0)  # nothing is linked yet
        self.link(self.show_a, 'fp-1')
        self.assertEqual(held.release_linked(submit), 1)
        self.assertEqual(held.release_linked(submit), 0)
        self.assertEqual(sent, [('main', {'count': 1})])
        self.assertEqual([row['frameio_project_id'] for row in self.waiting_rows()], ['fp-2'])

    def test_a_row_released_but_not_marked_is_sent_again_under_the_same_key(self) -> None:
        held = HeldActivity(self.registry)
        held.hold('fp-1', 'k1', {'count': 1})
        self.link(self.show_a, 'fp-1')
        keys: list[str] = []

        def crashing(queue: str, kind: str, key: str, payload: Mapping[str, Any]) -> object:
            keys.append(key)
            raise RuntimeError('crash before the row is marked')

        with self.assertRaises(RuntimeError):
            held.release_linked(crashing)
        held.release_linked(lambda queue, kind, key, payload: keys.append(key))
        self.assertEqual(len(keys), 2)
        self.assertEqual(len(set(keys)), 1)
        self.assertEqual(self.waiting_rows(), [])


class ReviewMessageHoldTests(unittest.TestCase):
    def job(self, project: str = 'fp-1') -> WorkerJob:
        payload: dict[str, Any] = {'file_id': 'f1', 'frameio_project_id': project, 'count': 1, 'first_at': 1_790_000_000.0, 'last_at': 1_790_000_001.0}
        return WorkerJob(1, 'main', 'frameio.review_done', 'k', payload, 't', 1)

    def answer(self, request: FrameioRequest) -> FrameioResult:
        return Ok({'id': 'f1', 'name': 'Cut'}) if isinstance(request, FileInfo) else Ok([])

    def test_without_a_hold_the_review_goes_to_the_default_chat_as_before(self) -> None:
        gateway = FakeGateway()
        ReviewMessage(GatewayClient(gateway, GATEWAY_SECRET), REVIEW_CHAT, None, self.answer).notify(self.job())
        self.assertEqual([message['chat'] for message in gateway.messages.values()], [REVIEW_CHAT])

    def test_a_linked_project_is_sent_and_never_held(self) -> None:
        gateway, held = FakeGateway(), []
        review = ReviewMessage(GatewayClient(gateway, GATEWAY_SECRET), REVIEW_CHAT, None, self.answer,
                               is_linked=lambda project: True, hold=lambda job, project: held.append(project))
        review.notify(self.job())
        self.assertEqual((held, len(gateway.messages)), ([], 1))

    def test_an_unlinked_project_is_held_and_nothing_is_sent(self) -> None:
        gateway, held = FakeGateway(), []
        review = ReviewMessage(GatewayClient(gateway, GATEWAY_SECRET), REVIEW_CHAT, None, self.answer,
                               is_linked=lambda project: False, hold=lambda job, project: held.append(project) or {'held': True})  # type: ignore[func-returns-value]
        self.assertEqual(review.notify(self.job()), {'held': True})
        self.assertEqual((held, len(gateway.messages)), (['fp-1'], 0))


class AccountScopeTests(LinkCase):
    """The list comes from the Frame.io worker's read API, which reads the configured T6 account only."""

    def reader_for(self, fake: FakeFrameio, account_id: str | None) -> FrameioReader:
        reads = FrameioReads(FrameioApi(StaticTokens(), fake.fetch, lambda _: None), account_id=account_id)
        api = ReadApi(FrameioWorker(object(), reads, lambda text, key: None), 'read-secret')  # type: ignore[arg-type]

        class Direct:
            def send(self, method: str, path: str, headers: Mapping[str, str], body: bytes) -> tuple[int, dict[str, Any]]:
                answer = api.handle(Request(method, path, dict(headers), body))
                return answer.status, json.loads(json.dumps(dict(answer.body)))

        return FrameioReader(Direct(), 'read-secret')

    def serve(self, name: str, reader: FrameioReader) -> str:
        self.service = OperatorService(self.registry, ActionStore(self.tmp.name + '/' + name), (OPERATOR,), people=self.people, frameio=reader)
        return self.open_frameio_list()

    def test_the_picker_shows_the_configured_accounts_projects_and_nothing_when_the_account_is_unclear(self) -> None:
        fake = FakeFrameio()
        fake.add_project('fp-1', 'Example Show')
        self.assertEqual(self.frameio_buttons(self.serve('a', self.reader_for(fake, 'acc-1'))), ['Example Show'])
        fake.accounts.append({'id': 'acc-other', 'display_name': 'Other account'})  # the sign-in now sees two accounts
        unclear = self.serve('b', self.reader_for(fake, None))
        self.assertEqual(self.frameio_buttons(unclear), [])
        self.assertIn('did not answer', self.body(unclear))
        other = self.serve('c', self.reader_for(fake, 'acc-other'))  # a different account is never mixed in
        self.assertEqual(self.frameio_buttons(other), [])


class FrameioWorkerBoundaryTests(unittest.TestCase):
    def test_the_frameio_worker_side_never_imports_the_t6_database_or_the_hold(self) -> None:
        root = Path(__file__).parents[1] / 'src' / 't6' / 'frameio'
        forbidden = ('sqlite3', 't6.registry', 't6.frameio_hold', 't6.media', 't6.person', 't6.operator')
        for name in ('worker.py', 'readapi.py', 'reads.py', 'api.py', 'tokens.py', 'client.py', 'requests.py', 'model.py'):
            tree = ast.parse((root / name).read_text())
            for node in ast.walk(tree):
                modules = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ''] if isinstance(node, ast.ImportFrom) else []
                for module in modules:
                    self.assertFalse(any(module == bad or module.startswith(bad + '.') for bad in forbidden), f'{name} imports {module}')


if __name__ == '__main__':
    unittest.main()
