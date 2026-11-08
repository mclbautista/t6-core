"""Credential withholding through the public ``t6.privacy.withhold`` seam.

Cases are ported from the watchdog masker in t6-jo-tg-ntn
(``jo_watchdog/core/masker.py`` and ``tests/test_masker.py``). The watchdog's
77-message corpus holds real job orders, so this public repository uses
synthetic job orders in the same shape instead.
"""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path
import subprocess
import sys
from typing import Any
import unittest

from t6.privacy import withhold

FIXTURES = Path(__file__).parent / "fixtures"
WITHHELD = "[password withheld – see Telegram message]"


def load_json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def masked(text: str) -> tuple[str, int]:
    result = withhold(text)
    return result.text, result.count


class WithholdResultTests(unittest.TestCase):
    def test_labelled_password_is_withheld_with_count_and_version(self) -> None:
        result = withhold("PW: secret")
        self.assertEqual(result.text, f"PW: {WITHHELD}")
        self.assertEqual(result.count, 1)
        self.assertEqual(result.masker_version, "1.0.0")

    def test_result_carries_nothing_that_could_restore_the_value(self) -> None:
        result = withhold("PW: secret\npass=other")
        self.assertEqual([field.name for field in fields(result)], ["text", "count", "masker_version"])
        self.assertNotIn("secret", repr(result))
        self.assertNotIn("other", repr(result))


class CredentialAssignmentTests(unittest.TestCase):
    def test_fixture_assignments_retain_label_and_mask_value(self) -> None:
        for source, expected, expected_count in load_json("withhold_cases.json")["positive"]:
            with self.subTest(source=source):
                self.assertEqual(masked(source), (expected, expected_count))

    def test_separator_must_immediately_follow_supported_label(self) -> None:
        for source in ["pw : secret", "pass = secret", "password : secret", "passcode = secret", "PIN : 1234"]:
            with self.subTest(source=source):
                self.assertEqual(masked(source), (source, 0))

    def test_separator_at_end_masks_next_non_blank_line(self) -> None:
        source = "PIN=\n \n\t\n1234\nDeadline: today"
        self.assertEqual(masked(source), (f"PIN=\n \n\t\n{WITHHELD}\nDeadline: today", 1))

    def test_separator_without_a_value_changes_nothing(self) -> None:
        for source in ["PW:", "password=\n", "pass:\n\n"]:
            with self.subTest(source=source):
                self.assertEqual(masked(source), (source, 0))

    def test_count_includes_each_withheld_assignment(self) -> None:
        source = "PW: alpha\nplain\npass=beta\nPIN:\n\n1234"
        expected = f"PW: {WITHHELD}\nplain\npass={WITHHELD}\nPIN:\n\n{WITHHELD}"
        self.assertEqual(masked(source), (expected, 3))

    def test_every_assignment_on_one_line_is_masked_and_counted(self) -> None:
        self.assertEqual(masked("PW: first pass: second"), (f"PW: {WITHHELD} pass: {WITHHELD}", 2))


class RequiredNotToMaskTests(unittest.TestCase):
    def test_every_negative_fixture_is_byte_for_byte_unchanged(self) -> None:
        for source in load_json("withhold_cases.json")["negative"]:
            with self.subTest(source=source):
                self.assertEqual(masked(source), (source, 0))

    def test_url_on_next_non_blank_line_is_not_consumed_as_a_value(self) -> None:
        for source in [
            "PW:\nhttps://example.com/?password=secret",
            "PW:\n  https://example.com/?password=secret  ",
            "pass=\n\nhttps://example.com/pw=secret&pin=1234",
            "PIN:\nwww.dropbox.com/s/abc?dl=0&rlkey=xyz",
            "Link: https://example.com/file#PW:secret",
        ]:
            with self.subTest(source=source):
                self.assertEqual(masked(source), (source, 0))

    def test_next_line_value_masks_non_url_text_but_preserves_url(self) -> None:
        source = "PW:\nsecret https://example.com/?password=link-key trailing"
        expected = f"PW:\n{WITHHELD} https://example.com/?password=link-key "
        self.assertEqual(masked(source), (expected, 1))
        self.assertEqual(masked(expected), (expected, 0))

    def test_url_inline_with_a_label_is_unchanged_and_not_dangling(self) -> None:
        source = "PW: https://example.com/?password=secret\nnot-a-password"
        self.assertEqual(masked(source), (source, 0))

    def test_url_is_preserved_while_assignment_outside_it_is_masked(self) -> None:
        source = "https://example.com/?password=secret PW: actual-secret"
        self.assertEqual(masked(source), (f"https://example.com/?password=secret PW: {WITHHELD}", 1))

    def test_inline_value_masks_around_preserved_url(self) -> None:
        cases = [
            (
                "PW: https://files.example/download secret-after-url",
                f"PW: https://files.example/download {WITHHELD}",
                1,
            ),
            (
                "PW: secret-before-url https://files.example/download trailing-secret",
                f"PW: {WITHHELD} https://files.example/download ",
                1,
            ),
            (
                "PW: first https://files.example/one trailing pass: https://files.example/two second",
                f"PW: {WITHHELD} https://files.example/one  pass: https://files.example/two {WITHHELD}",
                2,
            ),
        ]
        for source, expected, count in cases:
            with self.subTest(source=source):
                self.assertEqual(masked(source), (expected, count))
                self.assertEqual(masked(expected), (expected, 0))


class DeterminismTests(unittest.TestCase):
    def test_reapplying_is_idempotent_and_adds_no_count(self) -> None:
        first_text, first_count = masked("PW: secret")
        self.assertEqual((first_text, first_count), (f"PW: {WITHHELD}", 1))
        self.assertEqual(masked(first_text), (first_text, 0))

    def test_next_line_masking_preserves_indentation_and_is_idempotent(self) -> None:
        expected = f"PW:\n\n  {WITHHELD}"
        self.assertEqual(masked("PW:\n\n  secret"), (expected, 1))
        self.assertEqual(masked(expected), (expected, 0))

    def test_line_endings_are_preserved(self) -> None:
        self.assertEqual(masked("PW: secret\r\nnext\r\n"), (f"PW: {WITHHELD}\r\nnext\r\n", 1))

    def test_fixture_replacement_is_the_public_replacement(self) -> None:
        self.assertEqual(load_json("withhold_cases.json")["replacement"], WITHHELD)


class JobOrderTests(unittest.TestCase):
    def test_only_the_labelled_password_in_a_job_order_is_withheld(self) -> None:
        job_with_pw, *others = load_json("withhold_job_orders.json")
        expected = job_with_pw.replace("PW: hunter2", f"PW: {WITHHELD}")
        self.assertEqual(masked(job_with_pw), (expected, 1))
        self.assertIn("Link: https://files.example.test/share/abc", expected)
        for job in others:
            with self.subTest(first_line=job.splitlines()[0]):
                self.assertEqual(masked(job), (job, 0))


class IsolationTests(unittest.TestCase):
    def test_importing_privacy_loads_no_notion_or_network_modules(self) -> None:
        probe = (
            "import sys, json, t6.privacy\n"
            "print(json.dumps(sorted(m for m in sys.modules "
            "if m.startswith(('t6.notion', 'notion_client', 'http', 'urllib.request', 'socket')))))"
        )
        output = subprocess.run(
            [sys.executable, "-c", probe], check=True, capture_output=True, text=True
        ).stdout
        self.assertEqual(json.loads(output), [])


if __name__ == "__main__":
    unittest.main()
