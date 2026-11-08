"""Install the built distribution as base and Notion-only consumers."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import venv


class InstallationTests(unittest.TestCase):
    def test_base_and_notion_install_without_other_adapters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(
                [sys.executable, "-m", "hatchling", "build", "-t", "wheel", "-d", str(root)],
                check=True,
                capture_output=True,
                text=True,
            )
            wheel = next(root.glob("*.whl"))
            for extra in ("", "[notion]"):
                with self.subTest(extra=extra):
                    environment = root / ("notion" if extra else "base")
                    venv.create(environment, with_pip=True)
                    python = environment / "bin" / "python"
                    self.run_command(python, "-m", "pip", "install", "--no-index", f"{wheel}{extra}")
                    self.run_command(python, "-c", "import t6.domain; import t6.lookup")
                    installed = json.loads(self.run_command(python, "-c", self.distribution_probe()))
                    self.assertEqual(installed["dependencies"], [])
                    self.assertEqual(
                        set(installed["extras"]),
                        {"notion", "privacy", "llm", "frameio", "resolve", "instagantt", "server", "worker"},
                    )
                    # A fresh environment may include setuptools on Python 3.11.
                    self.assertLessEqual(set(installed["packages"]), {"t6", "pip", "setuptools"})
                    output = self.run_command(environment / "bin" / "t6", "version")
                    self.assertEqual(json.loads(output), {"version": "0.1.0"})

    @staticmethod
    def run_command(executable: Path, *arguments: str) -> str:
        return subprocess.run(
            [str(executable), *arguments], check=True, capture_output=True, text=True
        ).stdout

    @staticmethod
    def distribution_probe() -> str:
        return """
import json
from importlib.metadata import distributions, metadata, requires
print(json.dumps({
    "dependencies": requires("t6") or [],
    "extras": metadata("t6").get_all("Provides-Extra"),
    "packages": [distribution.metadata["Name"].lower() for distribution in distributions()],
}))
"""


if __name__ == "__main__":
    unittest.main()
