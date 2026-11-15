import tempfile
import unittest
from pathlib import Path
from typing import Any

from t6.transcribe.testing import FakeElevenLabs
from t6.worker.client import WorkerJob
from test_transcribe_folder import wav


class TranscriptWorkerTests(unittest.TestCase):
    def test_preview_measures_without_calling_elevenlabs(self) -> None:
        from t6.transcribe.worker import TranscriptWorker
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wav(root / 'one.wav', 2)
            reports = []
            client = FakeElevenLabs([])
            worker = TranscriptWorker(client, lambda job, kind, data: reports.append((kind, data)))
            job = WorkerJob(1, 'transcribe', 'transcript.preview', '1', {'audio_folder': str(root), 'formats': ['srt'], 'rate': 0.4}, 'lease', 1)
            worker.preview(job)
            self.assertEqual(len(client.calls), 0)
            self.assertEqual(reports[0][0], 'preview')
            self.assertEqual(reports[0][1]['files_found'], 1)
            self.assertAlmostEqual(reports[0][1]['hours_pending'], 2 / 3600)
            self.assertAlmostEqual(reports[0][1]['estimated_cost'], .4 * 2 / 3600)

    def test_run_reports_progress_and_keeps_outputs_under_audio(self) -> None:
        from t6.transcribe.worker import TranscriptWorker
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wav(root / 'one.wav')
            reports = []
            client = FakeElevenLabs([{'words': [{'type': 'word', 'text': 'Hi', 'start': 0, 'end': 1}]}])
            worker = TranscriptWorker(client, lambda job, kind, data: reports.append((kind, data)))
            payload: dict[str, Any] = {'audio_folder': str(root), 'formats': ['srt'], 'rate': 0.4, 'start_timecode': '01:00:00:00', 'no_diarize': True}
            job = WorkerJob(1, 'transcribe', 'transcript.preview', '1', payload, 'lease', 1)
            worker.preview(job)
            job = WorkerJob(2, 'transcribe', 'transcript.run', '2', {**payload, 'snapshot': reports[0][1]['snapshot']}, 'lease', 1)
            worker.run(job)
            self.assertFalse(client.calls[0]['diarize'])
            self.assertIn('01:00:00,000 --> 01:00:01,000', (root / '_transcripts/one.srt').read_text())
            self.assertEqual([item[0] for item in reports], ['preview', 'progress', 'complete'])
            self.assertEqual(reports[-1][1]['clips_done'], 1)

    def test_changed_audio_is_refused_before_call(self) -> None:
        from t6.transcribe.worker import TranscriptWorker
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wav(root / 'one.wav')
            reports = []
            client = FakeElevenLabs([])
            worker = TranscriptWorker(client, lambda job, kind, data: reports.append((kind, data)))
            payload = {'audio_folder': str(root)}
            job = WorkerJob(1, 'transcribe', 'transcript.preview', '1', payload, 'lease', 1)
            worker.preview(job)
            wav(root / 'one.wav', 2)
            job = WorkerJob(2, 'transcribe', 'transcript.run', '2', {**payload, 'snapshot': reports[0][1]['snapshot']}, 'lease', 1)
            worker.run(job)
            self.assertEqual(reports[-1], ('failed', {'rule': 'input_changed'}))
            self.assertEqual(client.calls, [])

    def test_max_hours_is_enforced_during_preview(self) -> None:
        from t6.transcribe.worker import TranscriptWorker
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wav(root / 'one.wav')
            worker = TranscriptWorker(FakeElevenLabs([]), lambda job, kind, data: None)
            job = WorkerJob(1, 'transcribe', 'transcript.preview', '1', {'audio_folder': str(root), 'max_hours': 0}, 'lease', 1)
            with self.assertRaisesRegex(ValueError, 'exceeds'):
                worker.preview(job)

    def test_changed_manifest_is_refused_before_call(self) -> None:
        from t6.transcribe.worker import TranscriptWorker
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wav(root / 'one.wav')
            manifest = root / 'manifest.csv'
            manifest.write_text('wav,note\none.wav,first\n')
            reports = []
            client = FakeElevenLabs([])
            worker = TranscriptWorker(client, lambda job, kind, data: reports.append((kind, data)))
            payload: dict[str, Any] = {'audio_folder': str(root), 'manifest': 'manifest.csv'}
            worker.preview(WorkerJob(1, 'transcribe', 'transcript.preview', '1', payload, 'lease', 1))
            manifest.write_text('wav,note\none.wav,changed\n')
            worker.run(WorkerJob(2, 'transcribe', 'transcript.run', '2', {**payload, 'snapshot': reports[0][1]['snapshot']}, 'lease', 1))
            self.assertEqual(reports[-1], ('failed', {'rule': 'input_changed'}))
            self.assertEqual(client.calls, [])

    def test_output_symlink_and_bad_timecode_are_refused_before_preview(self) -> None:
        from t6.transcribe.worker import TranscriptWorker
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wav(root / 'one.wav')
            worker = TranscriptWorker(FakeElevenLabs([]), lambda job, kind, data: None)
            (root / '_transcripts').symlink_to(root, target_is_directory=True)
            job = WorkerJob(1, 'transcribe', 'transcript.preview', '1', {'audio_folder': str(root)}, 'lease', 1)
            with self.assertRaises(ValueError):
                worker.preview(job)
            (root / '_transcripts').unlink()
            job = WorkerJob(1, 'transcribe', 'transcript.preview', '1', {'audio_folder': str(root), 'start_timecode': 'bad'}, 'lease', 1)
            with self.assertRaises(ValueError):
                worker.preview(job)

    def test_failed_clip_reports_failed_run(self) -> None:
        from t6.transcribe.worker import TranscriptWorker
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wav(root / 'one.wav')
            reports = []
            worker = TranscriptWorker(FakeElevenLabs([RuntimeError('offline')]), lambda job, kind, data: reports.append((kind, data)))
            payload: dict[str, Any] = {'audio_folder': str(root)}
            job = WorkerJob(1, 'transcribe', 'transcript.preview', '1', payload, 'lease', 1)
            worker.preview(job)
            job = WorkerJob(2, 'transcribe', 'transcript.run', '2', {**payload, 'snapshot': reports[0][1]['snapshot']}, 'lease', 1)
            worker.run(job)
            self.assertEqual(reports[-1][0], 'failed')
            self.assertEqual(reports[-1][1]['failed'], 1)
