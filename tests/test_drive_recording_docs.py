"""Decision and vocabulary contract for drive recording."""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
ADR = '0009-recording-channel.md'


class RecordingDecisionTests(unittest.TestCase):
    def test_recording_decision_is_accepted_and_links_its_sources(self) -> None:
        path = ROOT / 'docs' / 'adr' / ADR
        self.assertTrue(path.is_file(), 'ADR 0009 must record the accepted amendment')
        text = path.read_text()
        self.assertIn('Status: accepted', text)
        self.assertIn('(0007-no-chat-text.md)', text)
        self.assertIn('(https://github.com/mclbautista/t6-core/issues/615)', text)

    def test_original_decision_points_to_the_amendment(self) -> None:
        text = (ROOT / 'docs' / 'adr' / '0007-no-chat-text.md').read_text()
        self.assertIn(f'({ADR})', text)

    def test_drive_glossary_defines_each_new_term_once(self) -> None:
        text = (ROOT / 'CONTEXT.md').read_text()
        for term in ('Movement', 'Movement log', 'Capture', 'Recording channel',
                     'Reload', 'Reload report', 'Practice mode', 'Cutover', 'Place'):
            with self.subTest(term=term):
                definitions = re.findall(r'^- \*\*' + re.escape(term) + r'\*\*: (.+)', text, re.MULTILINE)
                self.assertEqual(len(definitions), 1, f'{term} needs one canonical definition')
        self.assertIn('TABLE, CABINET, SHELF, BOX', text)
        self.assertIn('QR labels are not used for recording', text)
