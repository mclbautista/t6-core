import tempfile
import unittest
from unittest.mock import patch
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
            worker = TranscriptWorker(client, lambda job, kind, data: reports.append((kind, data)), shares={'Studio': str(root)})
            job = WorkerJob(1, 'transcribe', 'transcript.preview', '1', {'audio_folder': '/Volumes/Studio', 'formats': ['srt'], 'rate': 0.4}, 'lease', 1)
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
            worker = TranscriptWorker(client, lambda job, kind, data: reports.append((kind, data)), shares={'Studio': str(root)})
            payload: dict[str, Any] = {'audio_folder': '/Volumes/Studio', 'formats': ['srt'], 'rate': 0.4, 'start_timecode': '01:00:00:00', 'no_diarize': True}
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
            worker = TranscriptWorker(client, lambda job, kind, data: reports.append((kind, data)), shares={'Studio': str(root)})
            payload = {'audio_folder': '/Volumes/Studio'}
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
            worker = TranscriptWorker(FakeElevenLabs([]), lambda job, kind, data: None, shares={'Studio': str(root)})
            job = WorkerJob(1, 'transcribe', 'transcript.preview', '1', {'audio_folder': '/Volumes/Studio', 'max_hours': 0}, 'lease', 1)
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
            worker = TranscriptWorker(client, lambda job, kind, data: reports.append((kind, data)), shares={'Studio': str(root)})
            payload: dict[str, Any] = {'audio_folder': '/Volumes/Studio', 'manifest': 'manifest.csv'}
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
            worker = TranscriptWorker(FakeElevenLabs([]), lambda job, kind, data: None, shares={'Studio': str(root)})
            (root / '_transcripts').symlink_to(root, target_is_directory=True)
            job = WorkerJob(1, 'transcribe', 'transcript.preview', '1', {'audio_folder': '/Volumes/Studio'}, 'lease', 1)
            with self.assertRaises(ValueError):
                worker.preview(job)
            (root / '_transcripts').unlink()
            job = WorkerJob(1, 'transcribe', 'transcript.preview', '1', {'audio_folder': '/Volumes/Studio', 'start_timecode': 'bad'}, 'lease', 1)
            with self.assertRaises(ValueError):
                worker.preview(job)

    def test_failed_clip_reports_failed_run(self) -> None:
        from t6.transcribe.worker import TranscriptWorker
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wav(root / 'one.wav')
            reports = []
            worker = TranscriptWorker(FakeElevenLabs([RuntimeError('offline')]), lambda job, kind, data: reports.append((kind, data)), shares={'Studio': str(root)})
            payload: dict[str, Any] = {'audio_folder': '/Volumes/Studio'}
            job = WorkerJob(1, 'transcribe', 'transcript.preview', '1', payload, 'lease', 1)
            worker.preview(job)
            job = WorkerJob(2, 'transcribe', 'transcript.run', '2', {**payload, 'snapshot': reports[0][1]['snapshot']}, 'lease', 1)
            worker.run(job)
            self.assertEqual(reports[-1][0], 'failed')
            self.assertEqual(reports[-1][1]['failed'], 1)


class ShareMappingTests(unittest.TestCase):
    KINDS = ('transcript.preview', 'transcript.run', 'transcript.speakers.preview', 'transcript.speakers.run', 'transcript.translate.preview', 'transcript.translate.run')

    def setUp(self) -> None:
        from t6.transcribe.worker import TranscriptWorker
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.mount = self.base / 'mount-alpha'
        self.elsewhere = self.base / 'elsewhere'
        (self.mount / 'sub').mkdir(parents=True)
        wav(self.elsewhere / 'one.wav')
        (self.elsewhere / '_transcripts').mkdir()
        (self.mount / 'link').symlink_to(self.elsewhere, target_is_directory=True)
        self.reports: list[tuple[str, dict[str, Any]]] = []
        self.client = FakeElevenLabs([])
        self.worker = TranscriptWorker(self.client, lambda job, kind, data: self.reports.append((kind, data)), shares={'Alpha': str(self.mount)})

    def handler(self, kind: str) -> Any:
        return {'transcript.preview': self.worker.preview, 'transcript.run': self.worker.run,
                'transcript.speakers.preview': self.worker.speakers_preview, 'transcript.speakers.run': self.worker.speakers_run,
                'transcript.translate.preview': self.worker.translate_preview, 'transcript.translate.run': self.worker.translate_run}[kind]

    def files(self) -> list[str]:
        return sorted(str(p.relative_to(self.base)) for p in self.base.rglob('*'))

    def test_folder_inside_mapped_share_runs_on_a_different_mount_point(self) -> None:
        folder = self.mount / 'Show' / 'day1'
        wav(folder / 'one.wav')
        payload: dict[str, Any] = {'audio_folder': '/Volumes/Alpha/Show/day1', 'formats': ['srt'], 'no_diarize': True}
        self.worker.preview(WorkerJob(1, 'transcribe', 'transcript.preview', '1', payload, 'lease', 1))
        self.assertEqual((self.reports[0][0], self.reports[0][1]['files_found']), ('preview', 1))
        self.assertFalse(Path('/Volumes/Alpha').exists())

    def test_mapped_share_run_writes_under_the_mount_point(self) -> None:
        from t6.transcribe.worker import TranscriptWorker
        wav(self.mount / 'one.wav')
        client = FakeElevenLabs([{'words': [{'type': 'word', 'text': 'Hi', 'start': 0, 'end': 1}]}])
        worker = TranscriptWorker(client, lambda job, kind, data: self.reports.append((kind, data)), shares={'Alpha': str(self.mount)})
        payload: dict[str, Any] = {'audio_folder': '/Volumes/Alpha', 'formats': ['srt'], 'no_diarize': True}
        worker.preview(WorkerJob(1, 'transcribe', 'transcript.preview', '1', payload, 'lease', 1))
        worker.run(WorkerJob(2, 'transcribe', 'transcript.run', '2', {**payload, 'snapshot': self.reports[0][1]['snapshot']}, 'lease', 1))
        self.assertTrue((self.mount / '_transcripts/one.srt').exists())
        self.assertEqual(self.reports[-1][0], 'complete')

    def test_mapped_share_runs_speakers_and_translate_folders(self) -> None:
        (self.mount / '_transcripts').mkdir()
        (self.mount / '_transcripts/one.srt').write_text('1\n00:00:01,000 --> 00:00:02,000\nHost:\nHello\n\n', encoding='utf-8')
        payload: dict[str, Any] = {'audio_folder': '/Volumes/Alpha', 'csv': 'source,speaker,name\n'}
        self.worker.speakers_preview(WorkerJob(1, 'transcribe', 'transcript.speakers.preview', '1', payload, 'lease', 1))
        self.worker.translate_preview(WorkerJob(2, 'transcribe', 'transcript.translate.preview', '2', payload, 'lease', 1))
        self.assertEqual([r[0] for r in self.reports], ['preview', 'preview'])
        self.assertEqual(self.reports[1][1]['clips'], ['one.srt'])

    def test_refused_refs_report_outside_shares_for_every_job_kind_and_touch_nothing(self) -> None:
        refs = {
            'unmapped share': '/Volumes/Beta/sub',
            'not under /Volumes': '/mnt/Alpha/sub',
            'relative': 'Alpha/sub',
            'bare volumes': '/Volumes',
            'volumes lookalike': '/Volumesx/Alpha/sub',
            '.. escape': '/Volumes/Alpha/sub/../../elsewhere',
            'symlink leaving the mount': '/Volumes/Alpha/link',
        }
        before = self.files()
        for label, ref in refs.items():
            for kind in self.KINDS:
                payload: dict[str, Any] = {'audio_folder': ref, 'csv': 'source,speaker,name\n', 'snapshot': 'x', 'rate': 0.4}
                self.reports.clear()
                with self.subTest(label=label, kind=kind), \
                        patch.object(Path, 'read_bytes', side_effect=AssertionError('read')), \
                        patch.object(Path, 'write_bytes', side_effect=AssertionError('write')), \
                        patch.object(Path, 'open', side_effect=AssertionError('open')), \
                        patch.object(Path, 'rglob', side_effect=AssertionError('walk')), \
                        patch.object(Path, 'iterdir', side_effect=AssertionError('walk')):
                    self.handler(kind)(WorkerJob(1, 'transcribe', kind, '1', payload, 'lease', 1))
                    self.assertEqual(self.reports, [('failed', {'rule': 'outside_shares'})])
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.files(), before)

    def test_mount_point_itself_is_inside_and_a_symlink_staying_inside_is_allowed(self) -> None:
        from t6.transcribe.worker import resolve_share_folder
        (self.mount / 'inner').symlink_to(self.mount / 'sub', target_is_directory=True)
        shares = {'Alpha': str(self.mount)}
        self.assertEqual(resolve_share_folder('/Volumes/Alpha', shares), self.mount)
        self.assertEqual(resolve_share_folder('/Volumes/Alpha/inner', shares), self.mount / 'sub')

