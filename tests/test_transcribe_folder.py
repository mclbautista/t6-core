import csv
import io
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path

from t6.transcribe.adapter import transcribe
from t6.transcribe.folder import preview_folder, run_folder
from t6.transcribe.testing import FakeElevenLabs


DATA = {'words': [{'type': 'word', 'text': 'Hello', 'start': 0, 'end': 0.5}]}


def wav(path, seconds=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as f:
        f.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        f.writeframes(b'\0\0' * 8000 * seconds)


def docx(_):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as z:
        z.writestr('[Content_Types].xml', '<Types/>')
        z.writestr('word/document.xml', '<document/>')
    return data.getvalue()


class FolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'audio'
        self.root.mkdir()

    def test_preview_selection_and_limits(self):
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

    def test_manifest_refusals(self):
        wav(self.root / 'a.wav')
        (self.root / 'x.txt').write_text('x')
        manifest = self.root / 'manifest.csv'
        for rows in ('../outside.wav', 'missing.wav', 'x.txt', 'a.wav\na.wav'):
            with self.subTest(rows=rows):
                manifest.write_text('wav\n' + rows + '\n')
                with self.assertRaises(ValueError):
                    preview_folder(self.root, manifest=manifest)

    def test_resume_failure_progress_and_docx(self):
        wav(self.root / 'Day/a.wav')
        wav(self.root / 'Day/b.wav', 2)
        wav(self.root / 'Day/c.wav', 3)
        client = FakeElevenLabs([DATA, RuntimeError('bad clip'), DATA])
        events = []
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
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(preview_folder(self.root, formats=('srt', 'docx')).pending, 0)

    def test_interruption_and_escape(self):
        wav(self.root / 'a.wav')
        wav(self.root / 'b.wav')
        client = FakeElevenLabs([DATA])

        def interrupt(path):
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
