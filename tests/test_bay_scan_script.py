"""The bay scan script (#379): one report on stdout, validated by the t6 report model, and nothing left behind."""
from __future__ import annotations

import ast
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from bay_scan_fixtures import library, project, snapshot
from t6 import bay_scanner
from t6.bay_scan import FORMAT_VERSION, BayScanReport

SCRIPT = Path(bay_scanner.__file__)
RESOLVE_PROCESS = "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/MacOS/Resolve"
NOW = datetime(2026, 11, 22, 2, 0, 4, tzinfo=timezone.utc)


class ScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        root = library(self.home)
        project(root, ("Group",), "Synthetic One", "uuid-1", 1_700_000_000, 2)
        project(root, (), "Synthetic Two", "uuid-2", 1_700_000_900, 0)

    def run_main(self, *, processes: list[str] | None = None, serial: str = "SERIAL-A") -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bay_scanner.main(["--home", str(self.home)], serial=lambda: serial, processes=lambda: processes or ["/sbin/launchd", "/usr/bin/ssh"],
                                    version=lambda: "20.3.1", now=lambda: NOW)
        return code, out.getvalue(), err.getvalue()

    def test_the_output_validates_against_the_t6_report_model(self) -> None:
        code, out, err = self.run_main()
        self.assertEqual((code, err), (0, ""))
        report = BayScanReport.model_validate_json(out)
        self.assertEqual((report.format_version, report.serial, report.status, report.resolve_version), (FORMAT_VERSION, "SERIAL-A", "ok", "20.3.1"))
        self.assertEqual(report.scanned_at, NOW)
        self.assertEqual([(copy.project_name, copy.folder_path, copy.timeline_count) for copy in report.local_copies],
                         [("Synthetic Two", "", 0), ("Synthetic One", "Group", 2)])
        self.assertEqual(out.count("\n"), 1)  # one JSON document on one line

    def test_the_script_and_the_model_agree_on_the_format_version(self) -> None:
        self.assertEqual(bay_scanner.FORMAT_VERSION, FORMAT_VERSION)

    def test_resolve_open_scans_nothing_says_skipped_and_exits_zero(self) -> None:
        with patch.object(bay_scanner, "scan_library", side_effect=AssertionError("must not scan")):
            code, out, _ = self.run_main(processes=["/sbin/launchd", RESOLVE_PROCESS])
        self.assertEqual(code, 0)
        report = BayScanReport.model_validate_json(out)
        self.assertEqual((report.status, report.local_copies, report.errors), ("skipped_resolve_open", [], []))

    def test_a_resolve_helper_outside_the_app_bundle_does_not_count_as_open(self) -> None:
        self.assertFalse(bay_scanner.resolve_is_open(["/Library/Application Support/Blackmagic Design/DaVinci Resolve/Panel Daemon", "/usr/bin/vim Resolve.txt"]))
        self.assertTrue(bay_scanner.resolve_is_open(["/Volumes/Apps/DaVinci Resolve.app/Contents/MacOS/Resolve"]))

    def test_the_library_is_unchanged_and_no_copy_is_left(self) -> None:
        before = snapshot(self.home)
        scratch = Path(self.tmp.name) / "scratch"
        scratch.mkdir()
        real = tempfile.mkdtemp
        made: list[str] = []

        def mkdtemp(prefix: str) -> str:
            made.append(real(prefix=prefix, dir=scratch))
            return made[-1]

        with patch("tempfile.mkdtemp", mkdtemp):
            self.run_main()
        self.assertEqual(snapshot(self.home), before)
        self.assertEqual(len(made), 1)
        self.assertFalse(Path(made[0]).exists())

    def test_a_failure_exits_non_zero_with_the_reason_on_stderr_and_prints_no_report(self) -> None:
        def no_serial() -> str:
            raise RuntimeError("cannot read the serial number: boom")

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bay_scanner.main(["--home", str(self.home)], serial=no_serial, processes=lambda: [])
        self.assertEqual((code, out.getvalue()), (1, ""))
        self.assertIn("boom", err.getvalue())

    def test_run_as_a_script_it_fails_cleanly_where_there_is_no_ioreg(self) -> None:
        done = subprocess.run([sys.executable, "-I", str(SCRIPT), "--home", str(self.home)], capture_output=True, text=True)
        if sys.platform == "darwin":
            self.assertEqual(done.returncode, 0)
            BayScanReport.model_validate_json(done.stdout)
        else:
            self.assertEqual((done.returncode, done.stdout), (1, ""))
            self.assertIn("serial number", done.stderr)

    def test_the_serial_is_read_from_ioreg_output(self) -> None:
        text = '+-o Root  <class IORegistryEntry>\n    "IOPlatformSerialNumber" = "SYNTH12345"\n    "other" = "x"\n'
        done = subprocess.CompletedProcess([], 0, stdout=text, stderr="")
        with patch("subprocess.run", return_value=done):
            self.assertEqual(bay_scanner.machine_serial(), "SYNTH12345")
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout="nothing", stderr="")):
            with self.assertRaises(RuntimeError):
                bay_scanner.machine_serial()

    def test_the_resolve_version_comes_from_the_app_plist_or_is_none(self) -> None:
        import plistlib
        plist = Path(self.tmp.name) / "Info.plist"
        plist.write_bytes(plistlib.dumps({"CFBundleShortVersionString": "20.3.1"}))
        self.assertEqual(bay_scanner.resolve_version(plist), "20.3.1")
        self.assertIsNone(bay_scanner.resolve_version(Path(self.tmp.name) / "missing.plist"))

    def test_the_script_imports_only_the_standard_library_and_runs_on_python_39(self) -> None:
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"), feature_version=(3, 9))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                imported.add((node.module or "").split(".")[0])
        self.assertTrue(imported)
        self.assertEqual(imported - set(sys.stdlib_module_names), set())
        self.assertNotIn("t6", imported)


if __name__ == "__main__":
    unittest.main()
