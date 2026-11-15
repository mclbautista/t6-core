"""The image's pip extras cover every third-party package the API and main worker import at run time."""

from pathlib import Path
import re
import tomllib
import unittest

ROOT = Path(__file__).resolve().parent.parent

# Run-time third-party import (lazy or not) -> (distribution, extra that provides it).
# Not in the image on purpose:
#   transcribe: the transcribe worker runs from its own virtualenv on Sixto (t6-netops#105);
#   drives:     openpyxl is only needed by the admin drive import CLI;
#   resolve:    no API or main-worker verb needs psycopg.
RUNTIME_IMPORTS = {
    "httpx": ("httpx", "notion"),
    "yaml": ("pyyaml", "server"),
    "jsonschema": ("jsonschema", "server"),
    "referencing": ("referencing", "server"),
    "rapidfuzz": ("rapidfuzz", "lookup"),
    "cryptography": ("cryptography", "google"),
}


def image_extras() -> set[str]:
    text = (ROOT / "Dockerfile").read_text()
    match = re.search(r'pip install[^"\n]*"\.\[([^\]]*)\]"', text)
    assert match, "no `pip install .[extras]` line in the Dockerfile"
    return {extra.strip() for extra in match.group(1).split(",")}


def requirement_name(requirement: str) -> str:
    return re.split(r"[<>=!~\[ ;]", requirement, maxsplit=1)[0].lower()


class ImageExtrasTests(unittest.TestCase):
    def test_image_installs_the_extra_of_every_runtime_import(self) -> None:
        extras = image_extras()
        missing = {name: extra for name, (_, extra) in RUNTIME_IMPORTS.items() if extra not in extras}
        self.assertEqual(missing, {}, "image extras missing for run-time imports")

    def test_table_matches_pyproject(self) -> None:
        optional = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["optional-dependencies"]
        for name, (distribution, extra) in RUNTIME_IMPORTS.items():
            with self.subTest(name=name):
                self.assertIn(distribution, {requirement_name(r) for r in optional[extra]})


if __name__ == "__main__":
    unittest.main()
