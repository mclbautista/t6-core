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
        code, report = run_check({"app/job.py": "from t6.client import Client\nprint('hi')\n"})
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
                self.assert_flags(source, "adapter-import")

    def test_the_line_of_the_finding_is_reported(self) -> None:
        self.assert_flags("import os\n\nurl = 'https://api.notion.com/v1'\n", "direct-http", line=3)

    def test_other_t6_imports_and_similar_hosts_pass(self) -> None:
        code, _ = run_check(
            {
                "a.py": "from t6.client import Client\n"
                "x = 'https://notapi.notion.com.evil.test/'\ny = 'api.notion.company'\n"
            }
        )
        self.assertEqual(code, 0)

    def test_each_t6_import_other_than_client_and_verbs_fails(self) -> None:
        for source, module in (
            ("import t6", "t6"),
            ("import t6.domain", "t6.domain"),
            ("import t6.domain.deadlines as d", "t6.domain.deadlines"),
            ("from t6.domain.job_order_rules import header", "t6.domain.job_order_rules"),
            ("from t6.domain import Contact", "t6.domain"),
            ("from t6 import domain", "t6.domain"),
            ("from t6 import client, privacy", "t6.privacy"),
            ("from t6 import *", "t6"),
            ("from t6.privacy.withhold import withhold", "t6.privacy.withhold"),
            ("from t6.frameio.client import FrameioReader", "t6.frameio.client"),
            ("def f():\n    import t6.vocabulary\n", "t6.vocabulary"),
        ):
            with self.subTest(source=source):
                code, report = run_check({"app/job.py": source})
                self.assertEqual(code, 1)
                ((finding,),) = [report["findings"]]
                self.assertEqual((finding["rule"], finding["detail"]), ("t6-import", f"imports {module}"))

    def test_client_and_verbs_imports_pass(self) -> None:
        source = (
            "import t6.client\nimport t6.client as c\nimport t6.verbs\nimport t6.verbs.extra\n"
            "from t6.client import Client\nfrom t6.client.http import Transport\nfrom t6.verbs import Anything, Ring\n"
            "from t6 import client\nfrom t6 import verbs\nfrom t6 import client, verbs\nfrom . import domain\nimport t6x.domain\nimport t60\n"
        )
        self.assertEqual(run_check({"a.py": source})[0], 0)

    def test_a_t6_import_in_a_test_file_is_reported(self) -> None:
        code, report = run_check({"tests/test_rules.py": "from t6.domain.deadlines import parse\n"})
        self.assertEqual(code, 1)
        self.assertEqual([(f["rule"], f["line"]) for f in report["findings"]], [("t6-import", 1)])
        self.assertTrue(str(report["findings"][0]["file"]).endswith("tests/test_rules.py"))

    def test_a_t6_import_allowlist_entry_is_refused(self) -> None:
        entry = {"name": "migration", "file": "a.py", "rule": "t6-import", "expires": "9999-12-31"}
        code, report = run_check({"a.py": "from t6.domain import Contact", ".t6-consumer-allowlist.json": json.dumps([entry])})
        self.assertEqual(code, 1)
        self.assertEqual(sorted(f["rule"] for f in report["findings"]), ["allowlist", "t6-import"])
        self.assertIn("unknown exception rule", str(report["findings"]))

    def test_adapter_imports_and_credentials_fail(self) -> None:
        for source in ("import t6.notion", "from t6 import notion", "import t6.llm.transport", "from t6.llm import config"):
            with self.subTest(source=source):
                self.assert_flags(source, "adapter-import")
        for key in ("T6_NOTION_TOKEN", "OPENAI_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"):
            with self.subTest(key=key):
                self.assert_flags(f"token = os.environ.get({key!r})", "credential")
                code, report = run_check({"config.yaml": f"key: {key}\n"})
                self.assertEqual((code, report["findings"][0]["rule"]), (1, "credential"))

    def test_every_notion_credential_name_fails(self) -> None:
        for key in ("NOTION_API_KEY", "NOTION_TOKEN", "T6_NOTION_API_KEY", "T6_NOTION_TOKEN"):
            with self.subTest(key=key):
                self.assert_flags(f"token = os.environ.get({key!r})", "credential")
                code, report = run_check({".env.example": f"{key}=\n"})
                self.assertEqual((code, [f["detail"] for f in report["findings"]]), (1, [f"references {key}"]))

    def test_look_alike_notion_names_pass(self) -> None:
        for name in ("T6_NOTION_WEBHOOK_TOKEN", "T6_NOTION_PROJECTS_DATA_SOURCE", "NOTION_TOKEN_FILE"):
            with self.subTest(name=name):
                code, _ = run_check({"a.py": f"x = os.environ.get({name!r})\n", ".env.example": f"{name}=\n"})
                self.assertEqual(code, 0)

    def test_frameio_mention_passes(self) -> None:
        self.assertEqual(run_check({"a.py": "message = 'Open in Frame.io'"})[0], 0)

    def test_allowlist_is_printed_and_expiry_enforced(self) -> None:
        entry = {"name": "migration", "file": "a.py", "rule": "adapter-import", "expires": "9999-12-31"}
        files = {"a.py": "import t6.notion", ".t6-consumer-allowlist.json": json.dumps([entry])}
        code, report = run_check(files)
        self.assertEqual((code, report["findings"], report["exceptions"]), (0, [], [entry]))
        entry["expires"] = "2000-01-01"
        files[".t6-consumer-allowlist.json"] = json.dumps([entry])
        code, report = run_check(files)
        self.assertEqual(code, 1)
        self.assertIn("expired", str(report["findings"]))
        self.assertEqual(report["exceptions"], [entry])

    def test_invalid_allowlist_fails(self) -> None:
        code, report = run_check({".t6-consumer-allowlist.json": "{}"})
        self.assertEqual((code, report["findings"][0]["rule"]), (1, "allowlist"))

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
