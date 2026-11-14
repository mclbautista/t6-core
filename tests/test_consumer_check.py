"""Exercise `t6 check-consumer` as an app's CI would: files in, JSON and exit status out."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


def run_check(files: dict[str, str]) -> tuple[int, dict[str, list[dict[str, object]]]]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name, text in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "t6", "admin", "consumer-check", str(root)], capture_output=True, text=True, check=False
        )
    return result.returncode, json.loads(result.stdout)


class CheckConsumerTests(unittest.TestCase):
    def assert_flags(self, source: str, rule: str, line: int = 1) -> None:
        code, report = run_check({"app/job.py": source})
        self.assertEqual(code, 1, source)
        ((finding,),) = [report["findings"]]
        self.assertEqual((finding["rule"], finding["line"]), (rule, line))
        self.assertTrue(str(finding["file"]).endswith("app/job.py"))

    def test_clean_app_passes(self) -> None:
        code, report = run_check({"app/job.py": "from t6.domain import Project\nprint('hi')\n"})
        self.assertEqual((code, report), (0, {"findings": []}))

    def test_each_banned_host_fails(self) -> None:
        for url in (
            "https://api.notion.com/v1/pages",
            "https://openrouter.ai/api/v1/chat/completions",
            "https://api.deepseek.com/chat/completions",
            "https://api.frame.io/v4/accounts",
            "https://ims-na1.adobelogin.com/ims/token/v3",
        ):
            with self.subTest(url=url):
                self.assert_flags(f"URL = {url!r}\n", "direct-http")

    def test_each_generated_import_form_fails(self) -> None:
        for source in (
            "import t6.notion.generated\n",
            "import t6.notion.generated as g\n",
            "from t6.notion.generated import Project\n",
            "from t6.notion import generated\n",
        ):
            with self.subTest(source=source):
                self.assert_flags(source, "generated-import")

    def test_the_line_of_the_finding_is_reported(self) -> None:
        self.assert_flags("import os\n\nurl = 'https://api.notion.com/v1'\n", "direct-http", line=3)

    def test_other_t6_imports_and_similar_hosts_pass(self) -> None:
        code, _ = run_check(
            {
                "a.py": "from t6.notion import NotionClient\nfrom t6.notion.codec import relation\n"
                "x = 'https://notapi.notion.com.evil.test/'\ny = 'api.notion.company'\n"
            }
        )
        self.assertEqual(code, 0)

    def test_docstrings_are_not_scanned(self) -> None:
        code, _ = run_check({"a.py": '"""Talks to api.notion.com through t6."""\n\n\ndef f():\n    """See openrouter.ai."""\n'})
        self.assertEqual(code, 0)

    def test_virtualenv_and_cache_directories_are_skipped(self) -> None:
        code, _ = run_check({".venv/lib/x.py": "u = 'https://api.notion.com'\n", "ok.py": "pass\n"})
        self.assertEqual(code, 0)

    def test_a_file_that_cannot_be_parsed_is_reported(self) -> None:
        code, report = run_check({"broken.py": "def (:\n"})
        self.assertEqual(code, 1)
        self.assertEqual(report["findings"][0]["rule"], "unreadable")

    def test_a_missing_path_cannot_run(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "t6", "admin", "consumer-check", "/no/such/dir"], capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
