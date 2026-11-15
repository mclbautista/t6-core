"""Transcription runs survive trouble (#357 group B): resume, dead jobs, retries, refusals, speaker maps."""
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from t6.ids import EntityKind, T6Id
from t6.person import People
from t6.preview import PreviewTokens
from t6.registry import Registered
from t6.server.app import Request
from t6.server.queue import JobQueue
from t6.server.verbs import VerbAPI
from t6.transcribe.testing import FakeElevenLabs
from t6.transcribe.worker import TranscribeQueue, TranscriptWorker
from t6.worker.client import WorkerJob
from t6.worker.runner import Worker
from test_person import MemoryRegistry
from test_transcribe_folder import wav

WORDS = {'words': [{'type': 'word', 'text': 'synthetic', 'start': 0, 'end': 1, 'speaker_id': 'speaker_0'}]}


class Interrupt(BaseException):
    """Stands in for a killed worker: it is not an Exception, so nothing in the run catches it."""


class InterruptingClient(FakeElevenLabs):
    def convert(self, **kwargs: Any) -> Any:
        if len(self.calls) == 1:
            self.calls.append({})
            raise Interrupt()
        return super().convert(**kwargs)


class ResumeTests(unittest.TestCase):
    def test_interrupted_run_resumes_with_the_same_snapshot_and_completes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wav(root / 'one.wav')
            wav(root / 'two.wav')
            reports: list[tuple[str, dict[str, Any]]] = []
            client = InterruptingClient([WORDS, WORDS])
            worker = TranscriptWorker(client, lambda job, kind, data: reports.append((kind, data)), shares={'Studio': str(root)})
            payload: dict[str, Any] = {'audio_folder': '/Volumes/Studio', 'formats': ['srt']}
            worker.preview(WorkerJob(1, 'transcribe', 'transcript.preview', '1', payload, 'lease', 1))
            job = WorkerJob(2, 'transcribe', 'transcript.run', '2', {**payload, 'snapshot': reports[0][1]['snapshot']}, 'lease', 1)
            with self.assertRaises(Interrupt):
                worker.run(job)
            self.assertTrue((root / '_transcripts/one.srt').exists())
            reports.clear()
            worker.run(WorkerJob(2, 'transcribe', 'transcript.run', '2', job.payload, 'lease2', 2))
            self.assertEqual(reports[-1][0], 'complete')
            self.assertTrue((root / '_transcripts/two.srt').exists())
            self.assertEqual(client.calls[2]['file'][:4], b'RIFF')


class Flow:
    """The verbs, the API, the worker queue and an in-memory registry, wired in process."""

    def __init__(self, test: unittest.TestCase, formats: list[str] | None = None) -> None:
        self.registry = MemoryRegistry()
        test.addCleanup(self.registry.db.close)
        self.registry._new_id = lambda: str(T6Id.mint(EntityKind.PROJECT))
        project = self.registry.register(actor='setup', idempotency_key='project')
        assert isinstance(project, Registered)
        self.project = project.project.id
        person = str(T6Id.mint(EntityKind.PERSON))
        self.media = str(T6Id.mint(EntityKind.MEDIA_LOCATION))
        directory = tempfile.TemporaryDirectory()
        test.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / 'audio'
        self.root.mkdir()
        self.formats = formats or ['srt', 'docx']
        with self.registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES (?, 'staff', '[]', '[]', NULL, 0, 1)", (person,))
            db.execute("INSERT INTO media_locations VALUES (?, ?, ?, 'nas-path', ?, 'master', 1, NULL)", (self.media, self.project, self.project, '/Volumes/Studio/audio'))
        self.api = VerbAPI(People(self.registry, PreviewTokens('signing')), JobQueue(Path(directory.name) / 'queue'), {'person-secret': person, 'worker-secret': 'transcribe'}, lambda _: None, translate_enabled=True)
        flow = self

        class InProcess:
            def send(self, method: str, path: str, headers: Any, body: bytes) -> tuple[int, dict[str, Any]]:
                response = flow.api(Request(method, path, headers, body))
                return response.status, dict(response.body)
        self.queue = TranscribeQueue(InProcess(), 'worker-secret')
        self.client = FakeElevenLabs([])
        self.worker = TranscriptWorker(self.client, self.queue.report, shares={'Studio': directory.name})
        self.runner = Worker(self.queue, 'transcribe', {'transcript.preview': self.worker.preview, 'transcript.run': self.worker.run,
                                                        'transcript.speakers.preview': self.worker.speakers_preview, 'transcript.speakers.run': self.worker.speakers_run,
                                                        'transcript.translate.preview': self.worker.translate_preview, 'transcript.translate.run': self.worker.translate_run})

    def request(self, path: str, data: dict[str, Any], secret: str = 'person-secret', headers: dict[str, str] | None = None) -> dict[str, Any]:
        return dict(self.api(Request('POST', path, {'Authorization': 'Bearer ' + secret, **(headers or {})}, json.dumps(data).encode())).body)

    def create(self, **extra: Any) -> dict[str, Any]:
        return self.request('/v1/transcript/create', {'media': self.media, 'formats': self.formats, **extra})

    def preview(self) -> dict[str, Any]:
        job_id = self.create()['result']['job_id']
        assert self.runner.run_once()
        return self.request('/v1/transcript/get', {'transcript': str(job_id)})

    def transcribe(self) -> str:
        """Preview, confirm and run to the end; return the Transcript id."""
        confirmed = self.create(confirm=self.preview()['token'])
        assert self.runner.run_once()
        return str(confirmed['result']['transcript_id'])

    def transcript(self, identifier: str) -> dict[str, Any]:
        return dict(self.request('/v1/transcript/get', {'transcript': identifier})['result']['transcript'])

    def speakers(self, identifier: str, csv: str) -> dict[str, Any]:
        job_id = self.request('/v1/transcript/speakers', {'transcript': identifier, 'csv': csv})['result']['job_id']
        assert self.runner.run_once()
        plan = self.request('/v1/transcript/get', {'transcript': str(job_id)})
        self.request('/v1/transcript/speakers', {'transcript': identifier, 'csv': csv, 'confirm': plan['token']})
        assert self.runner.run_once()
        return plan


class DeadAndFailedJobTests(unittest.TestCase):
    def test_job_that_ends_dead_marks_the_transcript_failed_and_unblocks_the_next_run(self) -> None:
        flow = Flow(self)
        wav(flow.root / 'one.wav')
        now = [1_000_000.0]
        flow.registry._clock = lambda: now[0]
        confirmed = flow.create(confirm=flow.preview()['token'])
        identifier = confirmed['result']['transcript_id']
        self.assertEqual(flow.transcript(identifier)['state'], 'running')
        for _ in range(5):
            flow.request('/v1/transcribe/claim', {}, 'worker-secret')
            now[0] += 400
        flow.request('/v1/transcribe/claim', {}, 'worker-secret')
        result = flow.transcript(identifier)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['history'][-1]['action'], 'failed')
        self.assertIn('transcript_failed', [e.action for e in flow.registry.activity(flow.project)])
        flow.request('/v1/transcribe/claim', {}, 'worker-secret')
        self.assertEqual([e['action'] for e in flow.transcript(identifier)['history']], ['created', 'failed'])
        again = flow.create(confirm=flow.preview()['token'])
        self.assertEqual(again['outcome'], 'done')
        flow.request('/v1/transcribe/claim', {}, 'worker-secret')
        self.assertEqual(flow.transcript(identifier)['state'], 'running')
    def kill_run(self, flow: Flow, now: list[float]) -> None:
        """Let a run job's lease expire until it is dead, without the claim-time sweep."""
        from t6.worker.internal_queue import InternalJobQueue
        queue = InternalJobQueue(flow.registry, clock=flow.registry._clock)
        for _ in range(6):
            queue.claim('transcribe', 300)
            now[0] += 400

    def test_create_speakers_and_translate_sweep_a_dead_run_before_they_look_for_a_running_one(self) -> None:
        flow = Flow(self, ['srt'])
        wav(flow.root / 'one.wav')
        now = [1_000_000.0]
        flow.registry._clock = lambda: now[0]
        tokens = [flow.preview()['token'] for _ in range(2)]
        identifier = flow.create(confirm=tokens[0])['result']['transcript_id']
        self.kill_run(flow, now)
        self.assertEqual(flow.transcript(identifier)['state'], 'running')
        csv = 'source,speaker,name\n,speaker_0,Character A\n'
        self.assertEqual(flow.request('/v1/transcript/speakers', {'transcript': identifier, 'csv': csv})['outcome'], 'done')
        self.assertEqual([e['action'] for e in flow.transcript(identifier)['history']], ['created', 'failed'])
        self.assertEqual(flow.request('/v1/transcript/translate', {'transcript': identifier})['outcome'], 'done')
        self.assertEqual([e['action'] for e in flow.transcript(identifier)['history']], ['created', 'failed'])
        self.assertEqual(flow.create(confirm=tokens[1])['outcome'], 'done')
        self.assertEqual([e['action'] for e in flow.transcript(identifier)['history']], ['created', 'failed', 're-run'])

    def test_a_repeated_lookup_writes_the_failed_entry_once(self) -> None:
        from t6.transcript import TranscriptStore
        flow = Flow(self, ['srt'])
        wav(flow.root / 'one.wav')
        now = [1_000_000.0]
        flow.registry._clock = lambda: now[0]
        identifier = flow.create(confirm=flow.preview()['token'])['result']['transcript_id']
        self.kill_run(flow, now)
        store = TranscriptStore(flow.registry)
        with flow.registry._transaction() as db:
            self.assertFalse(store.is_running(transcript=identifier, _db=db))
            self.assertFalse(store.is_running(transcript=identifier, _db=db))
            self.assertFalse(store.is_running(media=flow.media, _db=db))
        self.assertEqual([e['action'] for e in flow.transcript(identifier)['history']], ['created', 'failed'])
        self.assertEqual([e.action for e in flow.registry.activity(flow.project)].count('transcript_failed'), 1)

    def test_job_that_fails_for_any_reason_marks_the_transcript_failed(self) -> None:
        flow = Flow(self)
        wav(flow.root / 'one.wav')
        def broken(job: Any) -> None:
            raise RuntimeError('synthetic')
        flow.runner._handlers['transcript.run'] = broken
        confirmed = flow.create(confirm=flow.preview()['token'])
        self.assertTrue(flow.runner.run_once())
        result = flow.transcript(confirmed['result']['transcript_id'])
        self.assertEqual((result['state'], result['history'][-1]['action']), ('failed', 'failed'))


class ResumedSummaryTests(unittest.TestCase):
    def test_a_resumed_run_summary_counts_the_whole_run(self) -> None:
        flow = Flow(self, ['srt'])
        wav(flow.root / 'one.wav', 3600)
        wav(flow.root / 'two.wav', 3600)
        now = [1_000_000.0]
        flow.registry._clock = lambda: now[0]
        flow.worker.client = InterruptingClient([WORDS, WORDS])
        confirmed = flow.create(confirm=flow.preview()['token'])
        with self.assertRaises(Interrupt):
            flow.runner.run_once()
        self.assertTrue((flow.root / '_transcripts/one.srt').exists())
        now[0] += 400
        self.assertTrue(flow.runner.run_once())
        result = flow.transcript(confirmed['result']['transcript_id'])
        self.assertEqual(result['state'], 'done')
        self.assertEqual(result['summary'], {'clips_done': 2, 'failed': 0, 'skipped': 0, 'audio_hours': 2.0, 'estimated_cost': 0.8})


class IdempotencyTests(unittest.TestCase):
    def test_retried_preview_does_not_queue_a_second_job(self) -> None:
        flow = Flow(self)
        wav(flow.root / 'one.wav')
        first = flow.create(idempotency_key='k1')
        second = flow.create(idempotency_key='k1')
        self.assertEqual(first, second)
        with flow.registry._transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM internal_jobs WHERE kind='transcript.preview'").fetchone()[0], 1)
        other = flow.request('/v1/transcript/create', {'media': flow.media, 'language': 'en', 'idempotency_key': 'k1'})
        self.assertEqual(other['rule'], 'idempotency_key')
        self.assertEqual(flow.create(idempotency_key=' ')['rule'], 'idempotency_key')

    def test_retried_confirm_returns_the_first_result_and_header_key_works(self) -> None:
        flow = Flow(self)
        wav(flow.root / 'one.wav')
        token = flow.preview()['token']
        first = flow.create(confirm=token, idempotency_key='k2')
        self.assertEqual(first['outcome'], 'done')
        retry = flow.request('/v1/transcript/create', {'media': flow.media, 'formats': flow.formats, 'confirm': token}, headers={'Idempotency-Key': 'k2'})
        self.assertEqual(retry, first)
        self.assertEqual(flow.create(confirm=token)['rule'], 'token_used')
        with flow.registry._transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM internal_jobs WHERE kind='transcript.run'").fetchone()[0], 1)

    def test_speakers_and_translate_retries_are_idempotent(self) -> None:
        flow = Flow(self)
        wav(flow.root / 'one.wav')
        flow.client.outcomes.append(WORDS)
        identifier = flow.transcribe()
        csv = 'source,speaker,name\n,speaker_0,Character A\n'
        first = flow.request('/v1/transcript/speakers', {'transcript': identifier, 'csv': csv, 'idempotency_key': 'k3'})
        self.assertEqual(flow.request('/v1/transcript/speakers', {'transcript': identifier, 'csv': csv, 'idempotency_key': 'k3'}), first)
        self.assertTrue(flow.runner.run_once())
        token = flow.request('/v1/transcript/get', {'transcript': str(first['result']['job_id'])})['token']
        run = flow.request('/v1/transcript/speakers', {'transcript': identifier, 'csv': csv, 'confirm': token, 'idempotency_key': 'k3'})
        self.assertEqual(run['outcome'], 'done')
        self.assertEqual(flow.request('/v1/transcript/speakers', {'transcript': identifier, 'csv': csv, 'confirm': token, 'idempotency_key': 'k3'}), run)
        self.assertTrue(flow.runner.run_once())
        translate = flow.request('/v1/transcript/translate', {'transcript': identifier, 'idempotency_key': 'k4'})
        self.assertEqual(flow.request('/v1/transcript/translate', {'transcript': identifier, 'idempotency_key': 'k4'}), translate)
        with flow.registry._transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM internal_jobs WHERE kind IN ('transcript.speakers.run', 'transcript.translate.preview')").fetchone()[0], 2)


class PreviewRefusalTests(unittest.TestCase):
    def test_over_the_cap_says_rule_hours_cost_and_cap(self) -> None:
        flow = Flow(self)
        wav(flow.root / 'one.wav', 36)
        job_id = flow.create(max_hours=0.005)['result']['job_id']
        self.assertTrue(flow.runner.run_once())
        refusal = flow.request('/v1/transcript/get', {'transcript': str(job_id)})
        self.assertEqual((refusal['outcome'], refusal['rule']), ('refused', 'max_hours'))
        for text in ('0.010 h', '0.0040 USD', 'cap of 0.005 h'):
            self.assertIn(text, refusal['reason'])

    def test_unreadable_clip_is_named(self) -> None:
        flow = Flow(self)
        wav(flow.root / 'one.wav')
        (flow.root / 'broken.wav').write_bytes(b'not audio')
        job_id = flow.create()['result']['job_id']
        self.assertTrue(flow.runner.run_once())
        refusal = flow.request('/v1/transcript/get', {'transcript': str(job_id)})
        self.assertEqual((refusal['outcome'], refusal['rule']), ('refused', 'unreadable_clip'))
        self.assertIn('broken.wav', refusal['reason'])
        self.assertNotIn('one.wav', refusal['reason'])


class SpeakerMapTests(unittest.TestCase):
    CSV = 'source,speaker,name\n,speaker_0,Character A\n'

    def test_a_later_run_renders_new_clips_with_the_applied_map(self) -> None:
        flow = Flow(self, ['srt'])
        wav(flow.root / 'one.wav')
        flow.client.outcomes.append(WORDS)
        identifier = flow.transcribe()
        flow.speakers(identifier, self.CSV)
        wav(flow.root / 'two.wav')
        flow.client.outcomes.append(WORDS)
        self.assertEqual(flow.create(confirm=flow.preview()['token'])['outcome'], 'done')
        self.assertTrue(flow.runner.run_once())
        self.assertIn('Character A', (flow.root / '_transcripts/two.srt').read_text())

    def test_worker_run_applies_the_speaker_map_in_its_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wav(root / 'one.wav')
            reports: list[tuple[str, dict[str, Any]]] = []
            worker = TranscriptWorker(FakeElevenLabs([WORDS]), lambda job, kind, data: reports.append((kind, data)), shares={'Studio': directory})
            payload: dict[str, Any] = {'audio_folder': '/Volumes/Studio', 'formats': ['srt'], 'speaker_map': self.CSV}
            worker.preview(WorkerJob(1, 'transcribe', 'transcript.preview', '1', payload, 'lease', 1))
            worker.run(WorkerJob(2, 'transcribe', 'transcript.run', '2', {**payload, 'snapshot': reports[0][1]['snapshot']}, 'lease', 1))
            self.assertIn('Character A', (root / '_transcripts/one.srt').read_text())

    def test_clip_with_missing_audio_is_reported_and_still_rendered_from_stored_json(self) -> None:
        flow = Flow(self, ['srt'])
        wav(flow.root / 'one.wav')
        wav(flow.root / 'two.wav')
        flow.client.outcomes.extend([WORDS, WORDS])
        identifier = flow.transcribe()
        (flow.root / 'one.wav').unlink()
        plan = flow.speakers(identifier, self.CSV)
        self.assertEqual(plan['plan']['missing_audio'], ['one.wav'])
        self.assertEqual(sorted(c['source'] for c in plan['plan']['changes']), ['one.wav', 'two.wav'])
        self.assertIn('Character A', (flow.root / '_transcripts/one.srt').read_text())

    def test_docx_is_written_only_when_the_formats_include_it(self) -> None:
        flow = Flow(self, ['srt'])
        wav(flow.root / 'one.wav')
        flow.client.outcomes.append(WORDS)
        identifier = flow.transcribe()
        flow.speakers(identifier, self.CSV)
        self.assertIn('Character A', (flow.root / '_transcripts/one.srt').read_text())
        self.assertFalse((flow.root / '_transcripts/one.docx').exists())


if __name__ == '__main__':
    unittest.main()
