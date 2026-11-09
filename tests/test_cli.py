"""Exercise the CLI as a caller would, in a separate process."""

import json
import subprocess
import sys
import unittest


class VersionCommandTests(unittest.TestCase):
    def test_version_prints_json(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "t6", "version"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(json.loads(result.stdout), {"version": "0.2.0"})
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
