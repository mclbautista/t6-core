"""Compatibility of the documented command rename table."""
import contextlib
import io
import unittest
from unittest.mock import patch

from t6.cli import main


class RenameTests(unittest.TestCase):
    def test_renamed_commands_share_handlers_and_output(self):
        rows = [
            ("jo create", "jo raise", "--file x --data-sources x", "t6.cli_jo.run"),
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
        for old, new, options, handler in rows:
            with self.subTest(old=old), patch(handler) as run:
                def result(*args):
                    print('{"result": "same"}')
                    return 0
                run.side_effect = result
                outputs = []
                for command in (old, new):
                    out, err = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        status = main((command + " " + options).split())
                    outputs.append((status, out.getvalue(), err.getvalue()))
                self.assertEqual(outputs[0][:2], outputs[1][:2])
                self.assertEqual(outputs[1][2], "")
                self.assertEqual(outputs[0][2], f"Deprecated: t6 {old}; use t6 {new} until 0.5.0\n")
                self.assertEqual(run.call_count, 2)
                first, second = (call.args[0] for call in run.call_args_list)
                self.assertEqual(first.command, second.command)

    def test_admin_help_lists_commands(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            main(["admin", "--help"])
        for command in ("registry", "schema-check", "snapshot", "backup", "restore", "consumer-check", "drive"):
            self.assertIn(command, out.getvalue())
