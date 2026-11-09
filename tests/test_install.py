"""Install the built distribution as base and Notion-only consumers."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import venv

# httpx and what it pulls in. No Frame.io, Adobe sign-in or LLM libraries.
NOTION_PACKAGES = {
    "httpx", "httpcore", "h11", "anyio", "idna", "certifi", "sniffio",
    "typing-extensions", "typing_extensions", "exceptiongroup",
}


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
                    # The base package must install with no index at all. The
                    # Notion extra fetches httpx and its dependencies.
                    offline = [] if extra else ["--no-index"]
                    self.run_command(python, "-m", "pip", "install", *offline, f"{wheel}{extra}")
                    self.run_command(python, "-c", "import t6.domain; import t6.lookup; import t6.drives, t6.drives.testing")
                    installed = json.loads(self.run_command(python, "-c", self.distribution_probe()))
                    # Adapter libraries sit behind their own extra only.
                    self.assertEqual(
                        set(installed["dependencies"]),
                        {
                            "httpx<1,>=0.27; extra == 'notion'",
                            "pydantic<3,>=2.7; extra == 'llm'",
                            "psycopg[binary]<4,>=3.2; extra == 'resolve'",
                        },
                    )
                    self.assertEqual(
                        set(installed["extras"]),
                        {"notion", "privacy", "llm", "frameio", "resolve", "instagantt", "server", "worker"},
                    )
                    # A fresh environment may include setuptools on Python 3.11.
                    allowed = {"t6", "pip", "setuptools"}
                    if extra:
                        allowed |= NOTION_PACKAGES
                        self.run_command(python, "-c", "import t6.notion, t6.notion.testing")
                    self.assertLessEqual(set(installed["packages"]), allowed)
                    output = self.run_command(environment / "bin" / "t6", "version")
                    self.assertEqual(json.loads(output), {"version": "0.2.0"})

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
