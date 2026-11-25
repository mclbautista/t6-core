"""Plain t6 imports must work without optional image dependencies."""
import subprocess
import sys
import unittest


class OptionalDependencyTests(unittest.TestCase):
    def test_capture_server_and_client_import_without_pillow(self) -> None:
        for module in ('t6.drives.capture', 't6.server', 't6.client'):
            with self.subTest(module=module):
                result = subprocess.run(
                    [sys.executable, '-c',
                     f"import sys; sys.modules['PIL'] = None; import {module}"],
                    capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
