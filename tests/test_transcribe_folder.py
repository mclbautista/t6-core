import csv
import json
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path
from typing import Any

from t6.transcribe.adapter import transcribe
from t6.transcribe import render_docx
from t6.transcribe.folder import preview_folder, run_folder
from t6.transcribe.testing import FakeElevenLabs


DATA = {'words': [{'type': 'word', 'text': 'Hello', 'start': 0, 'end': 0.5}]}


def wav(path: Path, seconds: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as f:
        f.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        f.writeframes(b'\0\0' * 8000 * seconds)


def docx(data: dict[str, Any]) -> bytes:
    return render_docx(data)


class FolderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'audio'
        self.root.mkdir()

    def test_preview_selection_and_limits(self) -> None:
        wav(self.root / 'Day/Cam/take.WAV', 2)
        wav(self.root / '.hidden/a.wav')
        wav(self.root / '._junk.wav')
        wav(self.root / '.hidden.wav')
        p = preview_folder(self.root)
        self.assertEqual((p.found, p.pending, p.already_done), (1, 1, 0))
        self.assertAlmostEqual(p.pending_hours, 2 / 3600)
        self.assertAlmostEqual(p.estimated_cost, 0.4 * 2 / 3600)
        with self.assertRaisesRegex(ValueError, 'exceeds'):
            preview_folder(self.root, max_hours=0)
        (self.root / 'broken.wav').write_bytes(b'broken')
        with self.assertRaisesRegex(ValueError, 'broken.wav'):
            preview_folder(self.root)

    def test_manifest_refusals(self) -> None:
        wav(self.root / 'a.wav')
        (self.root / 'x.txt').write_text('x')
        manifest = self.root / 'manifest.csv'
        for rows in ('../outside.wav', 'missing.wav', 'x.txt', 'a.wav\na.wav'):
            with self.subTest(rows=rows):
                manifest.write_text('wav\n' + rows + '\n')
                with self.assertRaises(ValueError):
                    preview_folder(self.root, manifest=manifest)

    def test_resume_failure_progress_and_docx(self) -> None:
        wav(self.root / 'Day/a.wav')
        wav(self.root / 'Day/b.wav', 2)
        wav(self.root / 'Day/c.wav', 3)
        client = FakeElevenLabs([DATA, RuntimeError('bad clip'), DATA])
        events: list[dict[str, Any]] = []
        plan = preview_folder(self.root, formats=('srt', 'docx'))
        run_folder(plan, lambda path: transcribe(path, client), docx_renderer=docx, progress=events.append)
        self.assertEqual([e['state'] for e in events], ['completed', 'failed', 'completed'])
        self.assertEqual([e['processed_seconds'] for e in events], [1, 3, 6])
        self.assertTrue(all(e['total_seconds'] == 6 for e in events))
        self.assertTrue((self.root / '_transcripts/Day/a.json').is_file())
        with (self.root / '_transcription_log.csv').open() as f:
            self.assertEqual([r['status'] for r in csv.DictReader(f)], ['completed', 'failed', 'completed'])
        (self.root / '_transcripts/Day/c.docx').write_bytes(b'corrupt')
        resumed = preview_folder(self.root, formats=('srt', 'docx'))
        self.assertEqual((resumed.already_done, resumed.pending), (1, 2))
        client = FakeElevenLabs([DATA, DATA])
        run_folder(resumed, lambda path: transcribe(path, client), docx_renderer=docx)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(preview_folder(self.root, formats=('srt', 'docx')).pending, 0)

    def test_reuse_and_default_renderer_with_speaker_map(self) -> None:
        wav(self.root / 'a.wav')
        data = {'words': [{'type': 'word', 'text': 'Hi', 'start': 0, 'end': 1, 'speaker_id': 's'}]}
        run_folder(preview_folder(self.root, formats=('json',)), lambda _: data)
        def no_call(_: Path) -> dict[str, Any]:
            self.fail('retained JSON should be reused')
        run_folder(preview_folder(self.root, formats=('srt', 'docx')), no_call,
                   speaker_map={('a.wav', 's'): 'Alice'})
        self.assertEqual(preview_folder(self.root, formats=('srt', 'docx')).pending, 0)
        self.assertIn('Alice:', (self.root / '_transcripts/a.srt').read_text())
        with zipfile.ZipFile(self.root / '_transcripts/a.docx') as archive:
            self.assertIn(b'Alice', archive.read('word/document.xml'))

    def test_failed_render_never_marks_old_outputs_complete(self) -> None:
        wav(self.root / 'a.wav')
        run_folder(preview_folder(self.root, formats=('srt', 'docx')), lambda _: DATA)
        (self.root / '_transcripts/a.json').unlink()
        def fail(_: dict[str, Any]) -> bytes:
            raise RuntimeError('render failed')
        run_folder(preview_folder(self.root, formats=('srt', 'docx')), lambda _: DATA, docx_renderer=fail)
        self.assertEqual(preview_folder(self.root, formats=('srt', 'docx')).pending, 1)

    def test_invalid_json_and_well_formed_invalid_docx(self) -> None:
        wav(self.root / 'a.wav')
        run_folder(preview_folder(self.root, formats=('docx',)), lambda _: DATA)
        output = self.root / '_transcripts'
        with zipfile.ZipFile(output / 'a.docx', 'w') as archive:
            archive.writestr('[Content_Types].xml', '<Types/>')
            archive.writestr('word/document.xml', '<document/>')
        self.assertEqual(preview_folder(self.root, formats=('docx',)).pending, 1)
        (output / 'a.json').write_text(json.dumps({'words': 'invalid'}))
        calls: list[Path] = []
        def again(path: Path) -> dict[str, Any]:
            calls.append(path)
            return DATA
        run_folder(preview_folder(self.root, formats=('docx',)), again)
        self.assertEqual(len(calls), 1)
        self.assertEqual(preview_folder(self.root, formats=('docx',)).pending, 0)

    def test_interruption_and_escape(self) -> None:
        wav(self.root / 'a.wav')
        wav(self.root / 'b.wav')
        client = FakeElevenLabs([DATA])

        def interrupt(path: Path) -> dict[str, Any]:
            if path.name == 'b.wav':
                raise KeyboardInterrupt()
            return transcribe(path, client)

        with self.assertRaises(KeyboardInterrupt):
            run_folder(preview_folder(self.root), interrupt)
        self.assertEqual(preview_folder(self.root).pending, 1)
        outside = Path(self.tmp.name) / 'outside'
        outside.mkdir()
        (self.root / '_transcripts/b.srt').symlink_to(outside / 'b.srt')
        with self.assertRaisesRegex(ValueError, 'escapes'):
            preview_folder(self.root)
        self.assertFalse((outside / 'b.srt').exists())


if __name__ == '__main__':
    unittest.main()
