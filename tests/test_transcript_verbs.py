import unittest
from typing import Any

from t6.verbs import catalogue


class TranscriptVerbTests(unittest.TestCase):
    def test_cli_and_typed_client_dispatch_registered_verbs(self) -> None:
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from t6.cli import main
        from t6.client import Client
        from t6.verbs import Done, TranscriptCreateInput, TranscriptGetInput, TranscriptTranslateInput
        calls: list[tuple[str, str, Any]] = []
        def call(client: Any, noun: str, verb: str, inputs: Any) -> Any:
            calls.append((noun, verb, inputs))
            return Done(result={'job_id': 1})
        with patch.object(Client, 'call', new=call), redirect_stdout(io.StringIO()):
            self.assertEqual(main(['transcript', 'create', 't6m_fake', '--no-diarize', '--max-hours', '2', '--json']), 0)
            self.assertEqual(main(['transcript', 'get', '1', '--json']), 0)
            self.assertEqual(main(['transcript', 'translate', '1', '--confirm', 'token', '--json']), 0)
            Client().transcript_translate(TranscriptTranslateInput(transcript='1'))
            Client().transcript_create(TranscriptCreateInput(media='t6m_fake'))
            Client().transcript_get(TranscriptGetInput(transcript='1'))
        self.assertTrue(calls[0][2].no_diarize)
        self.assertEqual(calls[0][2].max_hours, 2)
        self.assertEqual([(noun, verb) for noun, verb, _ in calls], [('transcript', 'create'), ('transcript', 'get'), ('transcript', 'translate'), ('transcript', 'translate'), ('transcript', 'create'), ('transcript', 'get')])
        self.assertEqual(calls[2][2].confirm, 'token')

    def test_catalogue_exposes_typed_create_and_get(self) -> None:
        entries = {entry['verb']: entry for entry in catalogue() if entry['noun'] == 'transcript'}
        self.assertEqual(set(entries), {'create', 'get', 'speakers', 'translate'})
        create = entries['create']
        self.assertIn('media', create['input_schema']['properties'])
        self.assertIn('confirm', create['input_schema']['properties'])
        self.assertIn('job_id', create['output_schema']['properties'])
        self.assertIn('transcript', entries['get']['input_schema']['properties'])

    def test_create_queues_a_preview_for_authenticated_person(self) -> None:
        import json
        import tempfile
        from pathlib import Path
        from t6.ids import EntityKind, T6Id
        from t6.person import People
        from t6.preview import PreviewTokens
        from t6.registry import Registered
        from t6.server.app import Request
        from t6.server.queue import JobQueue
        from t6.server.verbs import VerbAPI
        from media_seed import link_media
        from test_person import MemoryRegistry
        registry = MemoryRegistry()
        self.addCleanup(registry.db.close)
        registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        project = registry.register(actor='setup', idempotency_key='project')
        assert isinstance(project, Registered)
        person = str(T6Id.mint(EntityKind.PERSON))
        with registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', NULL, 0, 1)", (person,))
        media = link_media(registry, project.project.id, '/synthetic')
        with tempfile.TemporaryDirectory() as directory:
            api = VerbAPI(People(registry, PreviewTokens('signing')), JobQueue(Path(directory) / 'queue'), {'secret': person}, lambda _: None)
            response: Any = api(Request('POST', '/v1/transcript/create', {'Authorization': 'Bearer secret'}, json.dumps({'media': media}).encode()))
            self.assertEqual(response.body['outcome'], 'done')
            job_id = response.body['result']['job_id']
            with registry._transaction() as db:
                row = db.execute('SELECT queue, kind FROM internal_jobs WHERE id=?', (job_id,)).fetchone()
            self.assertEqual(row, ('transcribe', 'transcript.preview'))

    def test_preview_result_can_be_confirmed_only_once(self) -> None:
        import json
        import tempfile
        from pathlib import Path
        from t6.ids import EntityKind, T6Id
        from t6.person import People
        from t6.preview import PreviewTokens
        from t6.registry import Registered
        from t6.server.app import Request
        from t6.server.queue import JobQueue
        from t6.server.verbs import VerbAPI
        from media_seed import link_media
        from test_person import MemoryRegistry
        registry = MemoryRegistry()
        self.addCleanup(registry.db.close)
        registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        project = registry.register(actor='setup', idempotency_key='project')
        assert isinstance(project, Registered)
        person = str(T6Id.mint(EntityKind.PERSON))
        with registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', NULL, 0, 1)", (person,))
        media = link_media(registry, project.project.id, '/synthetic')
        with tempfile.TemporaryDirectory() as directory:
            api = VerbAPI(People(registry, PreviewTokens('signing')), JobQueue(Path(directory) / 'queue'), {'person-secret': person, 'worker-secret': 'transcribe'}, lambda _: None)
            def call(path: str, data: dict, secret: str = 'person-secret') -> dict:  # type: ignore[type-arg]
                return dict(api(Request('POST', path, {'Authorization': 'Bearer ' + secret}, json.dumps(data).encode())).body)
            job_id = call('/v1/transcript/create', {'media': media})['result']['job_id']
            job = call('/v1/transcribe/claim', {}, 'worker-secret')['job']
            self.assertEqual(job['job_id'], job_id)
            plan = {'files_found': 1, 'already_done': 0, 'hours_pending': 1.0, 'estimated_cost': 0.4, 'snapshot': 'a' * 64}
            call('/v1/transcribe/report', {'job_id': job_id, 'token': job['token'], 'kind': 'preview', 'data': plan}, 'worker-secret')
            preview = call('/v1/transcript/get', {'transcript': str(job_id)})
            self.assertEqual(preview['outcome'], 'preview')
            token = preview['token']
            self.assertEqual(call('/v1/transcript/create', {'media': media, 'language': 'en', 'confirm': token})['outcome'], 'refused')
            confirmed = call('/v1/transcript/create', {'media': media, 'confirm': token})
            self.assertEqual(confirmed['outcome'], 'done')
            self.assertEqual(call('/v1/transcript/create', {'media': media, 'confirm': token})['rule'], 'token_used')

    def test_verb_worker_flow_progress_rerun_and_activity(self) -> None:
        import json
        import tempfile
        from pathlib import Path
        from t6.ids import EntityKind, T6Id
        from t6.person import People
        from t6.preview import PreviewTokens
        from t6.registry import Registered
        from t6.server.app import Request
        from t6.server.queue import JobQueue
        from t6.server.verbs import VerbAPI
        from t6.transcribe.testing import FakeElevenLabs
        from t6.transcribe.worker import TranscribeQueue, TranscriptWorker
        from t6.worker.runner import Worker
        from media_seed import link_media
        from test_person import MemoryRegistry
        from test_transcribe_folder import wav
        registry = MemoryRegistry()
        self.addCleanup(registry.db.close)
        registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        project = registry.register(actor='setup', idempotency_key='project')
        assert isinstance(project, Registered)
        person = str(T6Id.mint(EntityKind.PERSON))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'audio'
            root.mkdir()
            wav(root / 'one.WAV')
            with registry._transaction() as db:
                db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', NULL, 0, 1)", (person,))
            media = link_media(registry, project.project.id, '/Volumes/Studio/audio')
            api = VerbAPI(People(registry, PreviewTokens('signing')), JobQueue(Path(directory) / 'queue'), {'person-secret': person, 'worker-secret': 'transcribe'}, lambda _: None)
            class InProcess:
                def send(self, method: str, path: str, headers: Any, body: bytes) -> tuple[int, dict[str, Any]]:
                    response = api(Request(method, path, headers, body))
                    return response.status, dict(response.body)
            transport = InProcess()
            queue = TranscribeQueue(transport, 'worker-secret')
            client = FakeElevenLabs([{'words': [{'type': 'word', 'text': 'synthetic', 'start': 0, 'end': 1, 'speaker_id': 'speaker_0'}]}])
            progress: list[dict[str, Any]] = []
            def request(path: str, data: dict[str, Any]) -> dict[str, Any]:
                return dict(api(Request('POST', path, {'Authorization': 'Bearer person-secret'}, json.dumps(data).encode())).body)
            def report(job: Any, kind: str, data: dict[str, Any]) -> None:
                queue.report(job, kind, data)
                if kind == 'progress':
                    progress.append(request('/v1/transcript/get', {'transcript': job.payload['transcript_id']}))
            worker = TranscriptWorker(client, report, shares={'Studio': directory})
            runner = Worker(queue, 'transcribe', {'transcript.preview': worker.preview, 'transcript.run': worker.run, 'transcript.speakers.preview': worker.speakers_preview, 'transcript.speakers.run': worker.speakers_run})
            def preview() -> dict[str, Any]:
                job_id = request('/v1/transcript/create', {'media': media, 'formats': ['srt', 'docx']})['result']['job_id']
                self.assertTrue(runner.run_once())
                return request('/v1/transcript/get', {'transcript': str(job_id)})
            first = preview()
            api.transcribe_rate = 0.5
            confirmed = request('/v1/transcript/create', {'media': media, 'formats': ['srt', 'docx'], 'confirm': first['token']})
            identifier = confirmed['result']['transcript_id']
            self.assertTrue(runner.run_once())
            self.assertEqual(progress[0]['result']['transcript']['state'], 'running')
            result = request('/v1/transcript/get', {'transcript': identifier})['result']['transcript']
            self.assertEqual(result['summary']['clips_done'], 1)
            self.assertAlmostEqual(result['summary']['estimated_cost'], 0.4 / 3600)
            self.assertTrue((root / '_transcripts/one.srt').exists())
            import zipfile
            bad = request('/v1/transcript/speakers', {'transcript': identifier, 'csv': 'source,speaker,name\n,speaker_0,\n'})
            self.assertEqual(bad['outcome'], 'refused')
            self.assertIn('line 2', bad['reason'])
            for name, previous in [('Character A', 'speaker_0'), ('Character B', 'Character A')]:
                csv = f'source,speaker,name\n,speaker_0,{name}\n'
                job_id = request('/v1/transcript/speakers', {'transcript': identifier, 'csv': csv})['result']['job_id']
                self.assertTrue(runner.run_once())
                plan = request('/v1/transcript/get', {'transcript': str(job_id)})
                self.assertEqual(plan['plan']['changes'], [{'source': 'one.WAV', 'speaker': 'speaker_0', 'from': previous, 'to': name}])
                competing_id = request('/v1/transcript/speakers', {'transcript': identifier, 'csv': csv})['result']['job_id']
                self.assertTrue(runner.run_once())
                competing = request('/v1/transcript/get', {'transcript': str(competing_id)})
                run = request('/v1/transcript/speakers', {'transcript': identifier, 'csv': csv, 'confirm': plan['token']})
                self.assertEqual(request('/v1/transcript/speakers', {'transcript': identifier, 'csv': csv, 'confirm': competing['token']})['rule'], 'transcript_running')
                self.assertEqual(request('/v1/transcript/speakers', {'transcript': identifier, 'csv': csv, 'confirm': plan['token']})['rule'], 'token_used')
                self.assertTrue(runner.run_once())
                self.assertEqual(request('/v1/transcript/get', {'transcript': str(run['result']['job_id'])})['outcome'], 'done')
                self.assertIn(name, (root / '_transcripts/one.srt').read_text())
                with zipfile.ZipFile(root / '_transcripts/one.docx') as doc:
                    self.assertIn(name, doc.read('word/document.xml').decode())
            history = request('/v1/transcript/get', {'transcript': identifier})['result']['transcript']['history']
            self.assertEqual([entry['map'] for entry in history if entry['action'] == 'speakers changed'], ['source,speaker,name\n,speaker_0,Character A\n', 'source,speaker,name\n,speaker_0,Character B\n'])
            self.assertEqual([e.action for e in registry.activity(project.project.id)][:2], ['transcript_speakers changed'] * 2)
            self.assertEqual(len(client.calls), 1)
            second = preview()
            self.assertEqual(second['plan']['already_done'], 1)
            again = request('/v1/transcript/create', {'media': media, 'formats': ['srt', 'docx'], 'confirm': second['token']})
            self.assertEqual(again['result']['transcript_id'], identifier)
            self.assertTrue(runner.run_once())
            self.assertEqual(len(client.calls), 1)
            self.assertEqual([entry['action'] for entry in request('/v1/transcript/get', {'transcript': identifier})['result']['transcript']['history']], ['created', 'speakers changed', 'speakers changed', 're-run'])
            self.assertEqual([e.action for e in registry.activity(project.project.id)][:2], ['transcript_re-run', 'transcript_speakers changed'])
            wav(root / 'two.wav')
            client.outcomes.append(RuntimeError('synthetic failure'))
            third = preview()
            request('/v1/transcript/create', {'media': media, 'formats': ['srt', 'docx'], 'confirm': third['token']})
            self.assertTrue(runner.run_once())
            result = request('/v1/transcript/get', {'transcript': identifier})['result']['transcript']
            self.assertEqual(result['state'], 'failed')
            self.assertEqual(result['history'][-1]['action'], 'failed')
            self.assertEqual(registry.activity(project.project.id)[0].action, 'transcript_failed')
            changed = preview()
            wav(root / 'two.wav', 2)
            queued = request('/v1/transcript/create', {'media': media, 'formats': ['srt', 'docx'], 'confirm': changed['token']})
            self.assertTrue(runner.run_once())
            result = request('/v1/transcript/get', {'transcript': str(queued['result']['job_id'])})
            self.assertEqual(result['rule'], 'input_changed')
            self.assertEqual(len(client.calls), 2)
            database = '\n'.join(registry.db.iterdump())
            self.assertNotIn('synthetic failure', database)
            self.assertNotIn('"text": "synthetic"', database)
