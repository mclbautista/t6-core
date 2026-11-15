"""The documented command rename table: old names fail naming the new one, new names work."""
import contextlib
import io
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

    def test_old_names_fail_naming_the_new_name_and_new_names_work(self) -> None:
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
                old_run, new_run = outputs
                self.assertEqual(old_run[0], 2)
                self.assertEqual(old_run[1], "")
                self.assertIn(f"t6 {new}", old_run[2])
                self.assertIn(f"t6 {old} was renamed to t6 {new}", old_run[2])
                self.assertEqual((new_run[0], new_run[2]), (0, ""))
                self.assertEqual(run.call_count, 1)

    def test_catalogue_advertises_no_renamed_name(self) -> None:
        from t6.verbs import catalogue
        old = ('jo create', 'contacts', 'projects', 'cameras', 'camera update')
        advertised = [alias for entry in catalogue() for alias in entry['aliases']]
        self.assertEqual([alias for alias in advertised if alias.startswith(old)], [])

    def test_admin_help_lists_commands(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            main(["admin", "--help"])
        for command in ("registry", "schema-check", "snapshot", "backup", "restore", "consumer-check", "drive"):
            self.assertIn(command, out.getvalue())
