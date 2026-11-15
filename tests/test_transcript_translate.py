import json
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

from t6.llm.api import Done
from t6.llm.evals import Attempt, Case, RunRecord, run_evals
from t6.llm.testing import FakeProvider
from t6.privacy import Person, Project, Registry
from t6.server.llm import LLMService
from t6.transcribe.srt import render_srt as render_transcript_srt
from t6.transcribe.translate import TranslationError, parse_srt, render_srt, translate_cues, task_input
from t6.transcribe.translate_evals import CASES, MUST_PASS, OFFLINE_REPLIES, grade_translation, names_for

NAMES = frozenset({'Host', 'Guest'})
TWO_CUES = '1\n00:00:01,000 --> 00:00:02,000\nHost:\nKumusta\n\n2\n00:00:02,500 --> 00:00:04,000\nGuest:\nMaayos naman\n\n'


class CueTests(unittest.TestCase):
    def test_srt_round_trips_with_wrapped_speaker_labels_and_multiline_text(self) -> None:
        text = '1\n00:00:01,000 --> 00:00:02,000\nA very long synthetic speaker name that\nwraps:\nline one\nline two\n\n2\n00:00:03,000 --> 00:00:04,000\nplain\n\n'
        cues = parse_srt(text, {'A very long synthetic speaker name that wraps'})
        self.assertEqual(cues[0].speaker, 'A very long synthetic speaker name that\nwraps:')
        self.assertEqual(cues[0].text, 'line one\nline two')
        self.assertEqual(cues[1].speaker, '')
        self.assertEqual(render_srt(cues), text)

    def test_a_label_is_only_a_name_the_transcript_knows(self) -> None:
        text = '1\n00:00:01,000 --> 00:00:02,000\nSabi niya:\nOo na\n\n2\n00:00:03,000 --> 00:00:04,000\nHost:\nSige\n\n'
        self.assertEqual([(c.speaker, c.text) for c in parse_srt(text)], [('', 'Sabi niya:\nOo na'), ('', 'Host:\nSige')])
        self.assertEqual([(c.speaker, c.text) for c in parse_srt(text, {'Host'})], [('', 'Sabi niya:\nOo na'), ('Host:', 'Sige')])

    def test_with_a_map_applied_a_raw_speaker_id_is_a_label_and_is_not_translated(self) -> None:
        text = '1\n00:00:01,000 --> 00:00:02,000\nspeaker_3:\nOo na\n\n2\n00:00:03,000 --> 00:00:04,000\nspeaker_3:\nSige\n\n'
        self.assertEqual([(c.speaker, c.text) for c in parse_srt(text, {'Host'})], [('speaker_3:', 'Oo na'), ('speaker_3:', 'Sige')])
        self.assertEqual([(c.speaker, c.text) for c in parse_srt(text, set())], [('speaker_3:', 'Oo na'), ('speaker_3:', 'Sige')])
        self.assertEqual([c.speaker for c in parse_srt(text)], ['', ''])

    def test_every_label_the_renderer_writes_for_a_mapped_transcript_is_recognised(self) -> None:
        words = [{'type': 'word', 'text': 'Oo', 'start': 0.0, 'end': 0.5, 'speaker_id': 'speaker_0'},
                 {'type': 'word', 'text': 'na', 'start': 1.0, 'end': 1.5, 'speaker_id': 'speaker_7'}]
        out = render_transcript_srt({'words': words}, speaker_map={('', 'speaker_0'): 'Host'}, source='a.wav')
        self.assertEqual([(c.speaker, c.text) for c in parse_srt(out, {'Host'})], [('Host:', 'Oo'), ('speaker_7:', 'na')])

    def test_unlabelled_cue_with_a_line_ending_in_a_colon_is_translated_in_full(self) -> None:
        text = '1\n00:00:01,000 --> 00:00:02,000\nSabi niya sa akin\nang sagot ay ito:\nOo na\n\n'
        cue = parse_srt(text)[0]
        self.assertEqual((cue.speaker, cue.text), ('', 'Sabi niya sa akin\nang sagot ay ito:\nOo na'))
        self.assertEqual(render_srt(parse_srt(text)), text)

    def test_translate_cues_keeps_structure_and_rejects_bad_replies(self) -> None:
        cues = parse_srt(TWO_CUES, NAMES)
        result = translate_cues(cues, lambda text: text.upper())
        self.assertEqual([(c.index, c.timecode, c.speaker) for c in result], [(c.index, c.timecode, c.speaker) for c in cues])
        for bad in ('', '  ', 'two\nlines', 'a\n\nb'):
            def reply(_text: str, bad: str = bad) -> str:
                return bad
            with self.subTest(bad=bad), self.assertRaises(TranslationError):
                translate_cues(cues, reply)
        with self.assertRaises(TranslationError):
            translate_cues(cues, lambda _text: None)


class EvalCaseTests(unittest.TestCase):
    def test_cases_are_synthetic_complete_and_cover_the_ticket(self) -> None:
        self.assertEqual(len({c.id for c in CASES}), len(CASES))
        for case in CASES:
            self.assertEqual((case.task, case.provenance.kind), ('transcript translate', 'synthetic'))
            self.assertIsInstance(case.masked_input, str)
            self.assertEqual(case.grader, 'cue-structure')
        self.assertEqual({c.id for c in CASES}, set(OFFLINE_REPLIES))
        self.assertEqual(
            {c.id for c in CASES},
            {'taglish-terms', 'code-switching', 'speaker-labels', 'timecodes-line-breaks', 'numbers-dates',
             'profanity-slang', 'no-merge-no-invention', 'character-name', 'unlabelled-colon', 'unmapped-label', 'raw-speaker-id'})
        self.assertEqual(MUST_PASS, {c.id for c in CASES})

    def test_offline_harness_runs_cases_through_the_task_with_the_scripted_fake_llm(self) -> None:
        sent: list[str] = []

        def evaluate_offline(case: Case) -> Attempt:
            assert isinstance(case.masked_input, str)
            fake = FakeProvider()
            replies = OFFLINE_REPLIES[case.id]
            for reply in replies:
                fake.reply(json.dumps({'text': reply}), prompt_tokens=1, completion_tokens=1)
            service = LLMService({'fake': fake}, 'fake', Registry())

            def run_cue(text: str) -> str | None:
                outcome = service._run(task_input(text))
                assert isinstance(outcome, Done)
                value = outcome.result
                return str(value.output['text']) if value.output_valid else None

            output = render_srt(translate_cues(parse_srt(case.masked_input, names_for(case)), run_cue))
            sent.extend(str(m['content']) for call in fake.calls for m in call.messages)
            self.assertEqual(fake.pending, 0)
            result = grade_translation(case, output)
            return Attempt(result.passed, result.reason)

        record = RunRecord(model_id='fake', provider='fake', prompt_hash='p', schema_hash='s',
                           t6_version='1', date=date(2026, 11, 15), results=[])
        result = run_evals(CASES, record=record, evaluate=lambda c, p: Attempt(False, 'live not run'), provider=FakeProvider(),
                           cost_cap=1, must_pass=MUST_PASS, offline=True, offline_evaluate=evaluate_offline)
        self.assertEqual(result.status, 'offline')
        self.assertEqual([(r.case_id, r.passed) for r in result.results], [(c.id, True) for c in CASES])
        self.assertTrue(any('Kapitan' in text for text in sent))

    def test_code_graders_fail_each_structural_defect(self) -> None:
        case = next(c for c in CASES if c.id == 'speaker-labels')
        assert isinstance(case.masked_input, str)
        cues = parse_srt(case.masked_input, names_for(case))
        translated = translate_cues(cues, lambda t: t)
        self.assertTrue(grade_translation(case, render_srt(translated)).passed)
        import dataclasses
        defects = {
            'dropped': translated[:-1],
            'added': translated + [dataclasses.replace(translated[-1], index=str(len(translated) + 1))],
            'timecode': [dataclasses.replace(translated[0], timecode='00:00:09,000 --> 00:00:10,000')] + translated[1:],
            'speaker': [dataclasses.replace(translated[0], speaker='Other:')] + translated[1:],
            'merged': [dataclasses.replace(translated[0], text=translated[0].text + '\n' + translated[1].text)] + translated[2:],
            'lines': [dataclasses.replace(translated[0], text=translated[0].text + '\nextra')] + translated[1:],
        }
        for name, bad in defects.items():
            with self.subTest(name):
                self.assertFalse(grade_translation(case, render_srt(bad)).passed)
        self.assertFalse(grade_translation(case, 'not an srt').passed)
        numbers = next(c for c in CASES if c.id == 'numbers-dates')
        assert isinstance(numbers.masked_input, str)
        changed = render_srt(translate_cues(parse_srt(numbers.masked_input, names_for(numbers)), lambda t: 'no digits here'))
        self.assertFalse(grade_translation(numbers, changed).passed)
        terms = next(c for c in CASES if c.id == 'taglish-terms')
        assert isinstance(terms.masked_input, str)
        lost = render_srt(translate_cues(parse_srt(terms.masked_input, names_for(terms)), lambda t: 'nothing kept'))
        self.assertFalse(grade_translation(terms, lost).passed)


class TranslateFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        from t6.ids import EntityKind, T6Id
        from t6.person import People
        from t6.preview import PreviewTokens
        from t6.registry import Registered
        from t6.server.app import Request
        from t6.server.queue import JobQueue
        from t6.server.verbs import VerbAPI
        from t6.transcribe.worker import TranscribeQueue, TranscriptWorker
        from t6.worker.runner import Worker
        from test_person import MemoryRegistry
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        project = self.registry.register(actor='setup', idempotency_key='project')
        assert isinstance(project, Registered)
        self.project = project.project.id
        person = str(T6Id.mint(EntityKind.PERSON))
        media = str(T6Id.mint(EntityKind.MEDIA_LOCATION))
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / 'audio'
        (self.root / '_transcripts').mkdir(parents=True)
        self.identifier = str(T6Id.mint(EntityKind.TRANSCRIPT))
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', NULL, 0, 1)", (person,))
            db.execute("INSERT INTO media_locations VALUES (?, ?, ?, 'nas-path', ?, 'master', 1, NULL)", (media, self.project, self.project, '/Volumes/Studio/audio'))
            db.execute("INSERT INTO transcripts VALUES (?, ?, NULL, ?, '{}', 'done', '{}', '{}')", (self.identifier, media, self.project))
        self.api = VerbAPI(People(self.registry, PreviewTokens('signing')), JobQueue(Path(directory.name) / 'queue'),
                           {'person-secret': person, 'worker-secret': 'transcribe', 'other': 'worker'}, lambda _: None,
                           translate_enabled=True)
        api = self.api

        class InProcess:
            def send(self, method: str, path: str, headers: Any, body: bytes) -> tuple[int, dict[str, Any]]:
                response = api(Request(method, path, headers, body))
                return response.status, dict(response.body)

        self.Request = Request
        self.transport = InProcess()
        queue = TranscribeQueue(self.transport, 'worker-secret')
        worker = TranscriptWorker(None, queue.report, queue=queue, shares={'Studio': directory.name})
        self.runner = Worker(queue, 'transcribe', {'transcript.translate.preview': worker.translate_preview, 'transcript.translate.run': worker.translate_run})

    named = False

    def names(self, *names: str) -> None:
        with self.registry._transaction() as db:
            history = db.execute("INSERT INTO transcript_history (transcript_id, action, actor, at) VALUES (?, 'speakers changed', 'setup', 1)", (self.identifier,))
            db.execute('INSERT INTO transcript_speaker_maps VALUES (?, ?)', (history.lastrowid, 'source,speaker,name\n' + ''.join(f',s{n},{name}\n' for n, name in enumerate(names))))

    def fake(self, *replies: str, retention: Any = 'zdr_enforced', registry: Registry | None = None) -> FakeProvider:
        fake = FakeProvider(retention=retention)
        for reply in replies:
            fake.reply(json.dumps({'text': reply}), prompt_tokens=3, completion_tokens=2)
        self.api.llm_service = LLMService({'fake': fake}, 'fake', registry or Registry())
        return fake

    def request(self, path: str, data: dict[str, Any], secret: str = 'person-secret') -> dict[str, Any]:
        return dict(self.api(self.Request('POST', path, {'Authorization': f'Bearer {secret}'}, json.dumps(data).encode())).body)

    def translate(self, **extra: Any) -> dict[str, Any]:
        return self.request('/v1/transcript/translate', {'transcript': self.identifier, **extra})

    def preview(self) -> dict[str, Any]:
        job_id = self.translate()['result']['job_id']
        self.assertTrue(self.runner.run_once())
        return self.request('/v1/transcript/get', {'transcript': str(job_id)})

    def history(self) -> list[str]:
        return [e['action'] for e in self.request('/v1/transcript/get', {'transcript': self.identifier})['result']['transcript']['history'] if e['actor'] != 'setup']

    def source(self, text: str = TWO_CUES, name: str = 'one.srt', names: tuple[str, ...] = ('Host', 'Guest')) -> None:
        if names and not self.named:
            self.names(*names)
            self.named = True
        (self.root / '_transcripts' / name).write_text(text, encoding='utf-8')

    def test_disabled_by_default_returns_refused_with_the_rule(self) -> None:
        self.api.translate_enabled = False
        self.source()
        outcome = self.translate()
        self.assertEqual((outcome['outcome'], outcome['rule']), ('refused', 'translate_disabled'))
        self.assertIn('disabled', outcome['reason'])
        self.assertEqual(self.history(), [])

    def test_preview_then_confirm_writes_en_outputs_history_and_activity(self) -> None:
        self.source()
        fake = self.fake('Hello', 'I am fine')
        plan = self.preview()
        self.assertEqual(plan['outcome'], 'preview')
        self.assertEqual(plan['plan'], {'clips': ['one.srt'], 'cue_count': 2, 'estimated_tokens': plan['plan']['estimated_tokens']})
        self.assertGreater(plan['plan']['estimated_tokens'], 0)
        self.assertEqual((fake.pending, self.history()), (2, []))
        self.assertFalse((self.root / '_transcripts/one.en.srt').exists())
        run = self.translate(confirm=plan['token'])
        self.assertEqual(self.translate(confirm=plan['token'])['rule'], 'token_used')
        self.assertTrue(self.runner.run_once())
        self.assertEqual(self.request('/v1/transcript/get', {'transcript': str(run['result']['job_id'])})['outcome'], 'done')
        english = parse_srt((self.root / '_transcripts/one.en.srt').read_text(encoding='utf-8'), NAMES)
        original = parse_srt(TWO_CUES, NAMES)
        self.assertEqual([(c.index, c.timecode, c.speaker) for c in english], [(c.index, c.timecode, c.speaker) for c in original])
        self.assertEqual([c.text for c in english], ['Hello', 'I am fine'])
        self.assertEqual((self.root / '_transcripts/one.srt').read_text(), TWO_CUES)
        with zipfile.ZipFile(self.root / '_transcripts/one.en.docx') as doc:
            xml = doc.read('word/document.xml').decode()
        self.assertEqual(xml.count('<w:p>') + xml.count('<w:p '), 2)
        for expected in ('Host [00:00:01,000] Hello', 'Guest [00:00:02,500] I am fine'):
            self.assertIn(expected, xml)
        self.assertEqual(self.history(), ['translated'])
        self.assertEqual([e.action for e in self.registry.activity(self.project)][:1], ['transcript_translated'])
        self.assertEqual(sorted(p.name for p in self.root.rglob('*') if p.is_file()), ['one.en.docx', 'one.en.srt', 'one.srt'])

    def test_with_a_map_applied_a_raw_id_label_is_kept_and_only_its_dialogue_is_translated(self) -> None:
        self.source('1\n00:00:01,000 --> 00:00:02,000\nspeaker_3:\nOo na\n\n', names=('Host',))
        fake = self.fake('Yes')
        plan = self.preview()
        self.translate(confirm=plan['token'])
        self.assertTrue(self.runner.run_once())
        sent = '\n'.join(str(m['content']) for call in fake.calls for m in call.messages)
        self.assertNotIn('speaker_3', sent)
        self.assertEqual((self.root / '_transcripts/one.en.srt').read_text(encoding='utf-8'),
                         '1\n00:00:01,000 --> 00:00:02,000\nspeaker_3:\nYes\n\n')

    def test_without_an_applied_speaker_map_the_first_line_is_dialogue_and_is_translated(self) -> None:
        self.source('1\n00:00:01,000 --> 00:00:02,000\nSabi niya:\nOo na\n\n', names=())
        fake = self.fake('He said:\nYes')
        plan = self.preview()
        self.translate(confirm=plan['token'])
        self.assertTrue(self.runner.run_once())
        sent = '\n'.join(str(m['content']) for call in fake.calls for m in call.messages)
        self.assertIn('Sabi niya:', sent)
        self.assertEqual((self.root / '_transcripts/one.en.srt').read_text(encoding='utf-8'),
                         '1\n00:00:01,000 --> 00:00:02,000\nHe said:\nYes\n\n')

    def test_registered_names_never_reach_the_llm_and_are_restored(self) -> None:
        text = ('1\n00:00:01,000 --> 00:00:02,000\nHost:\nSabi ni Orsolya Fenwick, tapos na ang Project Zephyrine.\n\n'
                '2\n00:00:02,500 --> 00:00:04,000\nGuest:\nOo, sabi ni Kapitan Bagwis, password: hunter2xyz\n\n')
        self.source(text)
        registry = Registry(people=[Person('Orsolya Fenwick')], projects=[Project('Project Zephyrine', 'ZPH-9')])
        fake = self.fake('[person 1] said [project 1] is done.', 'Yes, Captain Bagwis said so.', registry=registry)
        plan = self.preview()
        self.translate(confirm=plan['token'])
        self.assertTrue(self.runner.run_once())
        sent = '\n'.join(str(m['content']) for call in fake.calls for m in call.messages)
        for secret in ('Orsolya', 'Fenwick', 'Zephyrine', 'ZPH-9', 'hunter2xyz'):
            self.assertNotIn(secret, sent)
        self.assertIn('Kapitan Bagwis', sent)
        output = (self.root / '_transcripts/one.en.srt').read_text(encoding='utf-8')
        self.assertIn('Orsolya Fenwick said Project Zephyrine is done.', output)
        self.assertIn('Captain Bagwis', output)
        self.assertNotIn('[person', output)

    def test_provider_failure_is_refused_and_writes_nothing(self) -> None:
        self.source()
        fake = self.fake()
        fake.timeout()
        plan = self.preview()
        run = self.translate(confirm=plan['token'])
        self.assertTrue(self.runner.run_once())
        outcome = self.request('/v1/transcript/get', {'transcript': str(run['result']['job_id'])})
        self.assertEqual((outcome['outcome'], outcome['rule']), ('refused', 'translation'))
        self.assertFalse((self.root / '_transcripts/one.en.srt').exists())
        self.assertEqual(self.history(), ['failed'])

    def test_changed_original_between_preview_and_confirm_is_refused(self) -> None:
        self.source()
        self.fake('Hello', 'Fine')
        plan = self.preview()
        self.source(TWO_CUES.replace('Kumusta', 'Kamusta'))
        run = self.translate(confirm=plan['token'])
        self.assertTrue(self.runner.run_once())
        outcome = self.request('/v1/transcript/get', {'transcript': str(run['result']['job_id'])})
        self.assertEqual(outcome['rule'], 'input_changed')
        self.assertEqual(self.history(), ['failed'])

    def test_reply_that_changes_line_count_fails_the_job(self) -> None:
        self.source()
        self.fake('One\nTwo', 'Fine')
        plan = self.preview()
        run = self.translate(confirm=plan['token'])
        self.assertTrue(self.runner.run_once())
        self.assertEqual(self.request('/v1/transcript/get', {'transcript': str(run['result']['job_id'])})['rule'], 'translation')
        self.assertEqual(self.history(), ['failed'])
        self.assertEqual(sorted(p.name for p in (self.root / '_transcripts').iterdir()), ['one.srt'])

    def test_llm_route_belongs_to_the_worker_with_a_live_translate_lease(self) -> None:
        self.source()
        self.fake('Hello')
        body = {'job_id': 1, 'token': 'x', 'text': 'Kumusta'}
        for secret in ('person-secret', 'other', 'worker-secret'):
            with self.subTest(secret=secret):
                self.assertEqual(self.api(self.Request('POST', '/v1/transcribe/llm', {'Authorization': f'Bearer {secret}'}, json.dumps(body).encode())).status, 403)


    def test_unmapped_share_through_the_api_is_refused_with_outside_shares(self) -> None:
        self.source()
        self.fake('Hello', 'Fine')
        with self.registry._transaction() as db:
            db.execute("UPDATE media_locations SET ref='/Volumes/Unlisted/audio'")
        job_id = self.translate()['result']['job_id']
        self.assertTrue(self.runner.run_once())
        outcome = self.request('/v1/transcript/get', {'transcript': str(job_id)})
        self.assertEqual((outcome['outcome'], outcome['rule']), ('refused', 'outside_shares'))
        self.assertEqual(sorted(p.name for p in self.root.rglob('*') if p.is_file()), ['one.srt'])


if __name__ == '__main__':
    unittest.main()
