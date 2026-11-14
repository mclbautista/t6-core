"""Compatibility of the documented command rename table."""
import contextlib
import io
import re
from pathlib import Path
import unittest
from typing import Any
from unittest.mock import patch

from t6.cli import main


class RenameTests(unittest.TestCase):
    def setUp(self) -> None:
        from unittest.mock import MagicMock, patch
        people = MagicMock()
        people.authorize.return_value = None
        people.registry.bound_entity.return_value = None
        patcher = patch('t6.verbs._people', return_value=people)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_renamed_commands_fail_and_replacements_work(self) -> None:
        rows = [
            ("jo create", "jo raise", "--file x --data-sources x", "t6.verbs.run"),
            ("contacts find", "person find", "name --data-sources x", "t6.cli_studio.run"),
            ("projects resolve", "project resolve", "name --data-sources x", "t6.cli_studio.run"),
            ("cameras get", "camera get", "--data-sources x", "t6.cli._cameras"),
            ("cameras update", "camera enrich", "id --data-sources x", "t6.cli._cameras"),
            ("migrate-projects", "admin registry migrate", "--data-sources x", "t6.cli._migrate_projects"),
            ("classify-projects", "admin registry classify", "--decisions x --actor a", "t6.cli._classify_projects"),
            ("check", "admin schema-check", "--data-sources x", "t6.cli._schema_check"),
            ("schema-check", "admin schema-check", "--data-sources x", "t6.cli._schema_check"),
            ("schema-snapshot", "admin snapshot", "--data-sources x --out x", "t6.cli._schema_snapshot"),
            ("backup", "admin backup", "--into x", "t6.cli._ops"),
            ("restore", "admin restore", "--from x", "t6.cli._ops"),
            ("check-consumer", "admin consumer-check", "x", "t6.cli._check_consumer"),
            ("drives-import-dry-run", "admin drive import", "x", "t6.cli._drives_import_dry_run"),
        ]
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
        table = readme.split("### CLI rename table", 1)[1].split("\n\n", 2)[2].split("\n\n", 1)[0]
        documented: set[tuple[str, str]] = set()
        for line in table.splitlines():
            columns = line.split("|")
            if len(columns) < 4 or "unchanged" in columns[2]:
                continue
            old_names = re.findall(r"`([^`]+)`", columns[1])
            new_names = re.findall(r"`([^`]+)`", columns[2])
            if len(new_names) == 1:
                new_names *= len(old_names)
            documented.update(zip(old_names, new_names))
        self.assertEqual(documented, {(old, new) for old, new, _, _ in rows})
        for old, new, options, handler in rows:
            remote = old in ("contacts find", "projects resolve", "cameras get", "cameras update")
            target = "t6.client.Client.call" if remote else handler
            with self.subTest(old=old), patch(target) as run:
                def result(*args: Any) -> int:
                    print('{"result": "same"}')
                    return 0
                if remote:
                    from t6.verbs import Done
                    run.return_value = Done(result={"result": "same"})
                else:
                    run.side_effect = result
                outputs = []
                for command in (old, new):
                    out, err = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch('sys.stdin', io.StringIO('{"name":"Camera"}')):
                        status = main((command + " " + options).split())
                    outputs.append((status, out.getvalue(), err.getvalue()))
                self.assertEqual(outputs[0][0], 2)
                self.assertIn(f"renamed to t6 {new}", outputs[0][2])
                self.assertEqual(outputs[1][0], 0)
                self.assertEqual(outputs[1][2], "")
                self.assertEqual(run.call_count, 1)

    def test_admin_help_lists_commands(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            main(["admin", "--help"])
        for command in ("registry", "schema-check", "snapshot", "backup", "restore", "consumer-check", "drive"):
            self.assertIn(command, out.getvalue())
