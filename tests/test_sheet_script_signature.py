"""The Concerns sheet's Apps Script (#737) signs a Submit the way the `sheets_hmac` verifier checks it.

The vectors were made with `openssl dgst -sha256 -hmac`, not with t6 code, and are also printed in
apps-script/concerns-sheet/README.md so the Apps Script side can be checked by hand."""
import unittest
from pathlib import Path

from t6.concern_export import COLUMNS
from t6.concern_sheet import ACTIONS
from t6.server.signatures import SHEET_SIGNATURE_HEADER, SHEET_TIMESTAMP_HEADER, WebhookRejected, verify_sheet

FOLDER = Path(__file__).resolve().parent.parent / 'apps-script' / 'concerns-sheet'
SECRET = 'example-submit-secret'
TIMESTAMP = '1764000000'
VECTORS = (
    ('{"id":"c1","email":"a@b.co","rows":[]}', '213ed9dca6c55c61957347dc137cf5da135724016c09bf6351d5170465f56d6b'),
    ('{"id":"click-1","email":"closer@example.com","rows":[{"concern":"c-1","revision":3,"action":"Close","note":"Café = ok"}]}',
     '3f5adb49bb93488f85503f73d2179a3bed7b24ee6b3899fbf736223a197e90ac'),
)


class SheetScriptSignatureTest(unittest.TestCase):
    def test_known_signatures_pass_the_verifier(self) -> None:
        for body, signature in VECTORS:
            with self.subTest(body=body):
                headers = {SHEET_TIMESTAMP_HEADER: TIMESTAMP, SHEET_SIGNATURE_HEADER: signature}
                verify_sheet(SECRET, headers, body.encode(), float(TIMESTAMP))

    def test_a_changed_body_or_secret_is_refused(self) -> None:
        body, signature = VECTORS[0]
        headers = {SHEET_TIMESTAMP_HEADER: TIMESTAMP, SHEET_SIGNATURE_HEADER: signature}
        for secret, sent in (('other-secret', body), (SECRET, body.replace('c1', 'c2'))):
            with self.assertRaises(WebhookRejected):
                verify_sheet(secret, headers, sent.encode(), float(TIMESTAMP))

    def test_readme_lists_the_same_vectors(self) -> None:
        readme = (FOLDER / 'README.md').read_text(encoding='utf-8')
        for text in (SECRET, TIMESTAMP, *(part for vector in VECTORS for part in vector)):
            self.assertIn(text, readme)

    def test_script_names_the_export_columns_and_headers_the_server_speaks(self) -> None:
        script = (FOLDER / 'Code.gs').read_text(encoding='utf-8')
        named = ('Concern id', 'revision', 'allowed actions', 'last result')
        self.assertTrue(set(named) <= set(COLUMNS))
        for text in (*named, ACTIONS[0], SHEET_TIMESTAMP_HEADER, SHEET_SIGNATURE_HEADER):
            self.assertIn(text.lower(), script.lower())

    def test_repo_holds_no_script_id_or_url(self) -> None:
        for path in FOLDER.iterdir():
            text = path.read_text(encoding='utf-8')
            self.assertNotIn('docs.google.com', text, path.name)
            self.assertNotIn('script.google.com/macros', text, path.name)
        self.assertFalse((FOLDER / '.clasp.json').exists())


if __name__ == '__main__':
    unittest.main()
