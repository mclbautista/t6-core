"""Exercise the public-repo safety check through its command line."""

import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "public_repo_safety.py"


class SafetyCheckTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.repo = Path(self._directory.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        self.write("README.md", "Hello\n")

    def git(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", *arguments], cwd=self.repo, check=True, capture_output=True, text=True
        ).stdout

    def write(self, relative: str, content: str) -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        self.git("add", "-f", relative)

    def commit(self) -> str:
        self.git("commit", "-q", "-m", "change")
        return self.git("rev-parse", "HEAD").strip()

    def check(
        self, *arguments: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        environment = {
            key: value for key, value in os.environ.items() if not key.startswith("T6_")
        }
        environment.update(env or {})
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=self.repo,
            env=environment,
            capture_output=True,
            text=True,
        )


class ForbiddenPathTests(SafetyCheckTestCase):
    def test_clean_repository_passes(self) -> None:
        self.write(".env.example", "TOKEN=\n")
        self.write("config.example.yaml", "key: value\n")
        result = self.check("--skip-secrets")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_each_forbidden_path_fails(self) -> None:
        forbidden = [
            ".env",
            "app/.env",
            ".env.local",
            "cache.db",
            "nested/store.sqlite",
            "store.sqlite3",
            "config.yaml",
            "deploy/config.yml",
            "data/report.csv",
            "src/data/clients.json",
            "token.json",
            "frameio_tokens.json",
            "adobe-token.json",
            "notion.token",
        ]
        for path in forbidden:
            with self.subTest(path=path):
                self.write(path, "x\n")
                result = self.check("--skip-secrets")
                self.assertEqual(result.returncode, 1)
                self.assertIn(path, result.stdout)
                self.git("rm", "-q", "--cached", path)

    def test_untracked_files_are_ignored(self) -> None:
        (self.repo / ".env").write_text("SECRET=1\n")
        result = self.check("--skip-secrets")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class NamesTests(SafetyCheckTestCase):
    def test_match_prints_file_and_line_but_never_the_name(self) -> None:
        self.write("notes/meeting.md", "Agenda\nCall with Acme Pictures today\n")
        result = self.check(
            "--skip-secrets", env={"T6_NAMES_DENYLIST": "Acme Pictures\nOther Client\n"}
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("notes/meeting.md:2", result.stdout)
        output = result.stdout + result.stderr
        self.assertNotIn("Acme", output)
        self.assertNotIn("Other Client", output)

    def test_match_is_case_insensitive_and_whole_word(self) -> None:
        self.write("a.txt", "ACME PICTURES\n")
        self.write("b.txt", "acmepicturesque\n")
        result = self.check("--skip-secrets", env={"T6_NAMES_DENYLIST": "Acme Pictures"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("a.txt:1", result.stdout)
        self.assertNotIn("b.txt", result.stdout)

    def test_names_file_option(self) -> None:
        self.write("a.txt", "Jane Example signed off\n")
        names = Path(self._directory.name) / "names.txt"
        names.write_text("# comment\n\nJane Example\n")
        result = self.check("--skip-secrets", "--names-file", str(names))
        self.assertEqual(result.returncode, 1)
        self.assertIn("a.txt:1", result.stdout)

    def test_case_file_with_denied_name_is_rejected(self) -> None:
        self.write("evaluations/cases/example.json", '{"input": "Jane Example"}\n')
        names = Path(self._directory.name) / "names.txt"
        names.write_text("Jane Example\n")
        result = self.check("--skip-secrets", "--require-names", env={"T6_NAMES_DENYLIST_FILE": str(names)})
        self.assertEqual(result.returncode, 1)
        self.assertIn("evaluations/cases/example.json:1", result.stdout)
        self.assertNotIn("Jane Example", result.stdout + result.stderr)

    def test_denylist_file_inside_repository_is_rejected(self) -> None:
        self.write("names.txt", "Jane Example\n")
        result = self.check("--skip-secrets", "--names-file", str(self.repo / "names.txt"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("outside the repository", result.stdout)

    def test_missing_denylist_is_skipped_locally(self) -> None:
        result = self.check("--skip-secrets")
        self.assertEqual(result.returncode, 0)
        self.assertIn("names check skipped", result.stdout)

    def test_missing_denylist_fails_when_required(self) -> None:
        result = self.check("--skip-secrets", "--require-names")
        self.assertEqual(result.returncode, 1)
        self.assertIn("T6_NAMES_DENYLIST", result.stdout)

    def test_binary_files_are_skipped(self) -> None:
        path = self.repo / "image.png"
        path.write_bytes(b"\x89PNG\x00Acme Pictures")
        self.git("add", "image.png")
        result = self.check("--skip-secrets", env={"T6_NAMES_DENYLIST": "Acme Pictures"})
        self.assertEqual(result.returncode, 0, result.stdout)


class SecretScanTests(SafetyCheckTestCase):
    def fake_gitleaks(self, exit_code: int) -> tuple[Path, Path]:
        record = Path(self._directory.name) / "gitleaks-args"
        script = Path(self._directory.name) / "gitleaks"
        script.write_text(
            f'#!/bin/sh\nprintf "%s\\n" "$@" > "{record}"\nexit {exit_code}\n'
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return script, record

    def test_missing_scanner_fails_with_instructions(self) -> None:
        result = self.check(env={"T6_GITLEAKS": "/nonexistent/gitleaks"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("gitleaks", result.stdout)

    def test_scanner_finding_fails_and_range_is_passed(self) -> None:
        first = self.commit()
        self.write("x.txt", "more\n")
        second = self.commit()
        scanner, record = self.fake_gitleaks(exit_code=1)
        result = self.check("--range", f"{first}..{second}", env={"T6_GITLEAKS": str(scanner)})
        self.assertEqual(result.returncode, 1)
        arguments = record.read_text().splitlines()
        self.assertEqual(arguments[0], "git")
        self.assertIn(f"--log-opts={first}..{second}", arguments)
        self.assertIn("--redact", arguments)

    def test_clean_scan_passes_and_full_history_has_no_range(self) -> None:
        self.commit()
        scanner, record = self.fake_gitleaks(exit_code=0)
        result = self.check("--full-history", env={"T6_GITLEAKS": str(scanner)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(
            any(argument.startswith("--log-opts") for argument in record.read_text().splitlines())
        )


if __name__ == "__main__":
    unittest.main()
