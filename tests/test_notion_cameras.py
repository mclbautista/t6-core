"""Exercise `t6 cameras get` and `t6 cameras update` against the in-memory fake."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from t6.cli import main
from t6.notion.testing import FakeNotion, title

CAMERAS = "ds-cameras"


class CamerasCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeNotion()
        self.fake.add_snapshot_data_source(CAMERAS, "cameras")
        self.page = self.fake.add_page(
            CAMERAS, {"Name": title("FX6"), "Tags": {"type": "multi_select", "multi_select": [{"name": "Sony"}]}}
        )

    def run_command(
        self,
        argv: list[str],
        *,
        stdin: str = "",
        data_sources: dict[str, str] | None = None,
        token: str | None = "secret",
    ) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data-sources.json"
            path.write_text(json.dumps(data_sources or {"cameras": CAMERAS}), encoding="utf-8")
            environment = {"T6_NOTION_TOKEN": token} if token else {}
            out, err = io.StringIO(), io.StringIO()
            with (
                mock.patch.dict(os.environ, environment, clear=True),
                mock.patch.object(sys, "stdin", io.StringIO(stdin)),
                redirect_stdout(out),
                redirect_stderr(err),
            ):
                status = main(
                    [*argv, "--data-sources", str(path)],
                    open_notion=lambda _token: self.fake.client(),
                )
        return status, out.getvalue(), err.getvalue()

    def test_get_prints_cameras_with_domain_names(self) -> None:
        status, out, err = self.run_command(["camera", "get"])
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(
            json.loads(out),
            [
                {
                    "id": self.page["id"],
                    "name": "FX6",
                    "related_to_projects_cameras": [],
                    "tags": ["Sony"],
                    "wiki": [],
                }
            ],
        )

    def test_get_one_camera_by_page_id(self) -> None:
        self.fake.add_page(CAMERAS, {"Name": title("A7S")})
        status, out, _ = self.run_command(["camera", "get", "--page-id", self.page["id"]])
        self.assertEqual(status, 0)
        self.assertEqual([row["name"] for row in json.loads(out)], ["FX6"])

    def test_update_writes_and_prints_the_updated_row(self) -> None:
        payload = json.dumps({"name": "FX6 Mk2", "tags": ["Sony", "Full frame"]})
        status, out, err = self.run_command(["camera", "enrich", self.page["id"]], stdin=payload)
        self.assertEqual((status, err), (0, ""))
        row = json.loads(out)
        self.assertEqual((row["name"], row["tags"]), ("FX6 Mk2", ["Sony", "Full frame"]))
        stored = self.fake.page(self.page["id"])["properties"]
        self.assertEqual(stored["Name"]["title"][0]["text"]["content"], "FX6 Mk2")
        self.assertIn("Full frame", self.fake.select_options(CAMERAS, "Tags"))

    def test_update_reads_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "update.json"
            path.write_text(json.dumps({"name": "Renamed"}), encoding="utf-8")
            status, out, _ = self.run_command(
                ["camera", "enrich", self.page["id"], "--json", str(path)]
            )
        self.assertEqual((status, json.loads(out)["name"]), (0, "Renamed"))

    def assert_fails(self, argv: list[str], stdin: str, text: str) -> None:
        before = self.fake.page(self.page["id"])
        status, out, err = self.run_command(argv, stdin=stdin)
        self.assertEqual(status, 2)
        self.assertEqual(out, "")
        self.assertIn(text, json.loads(err)["error"])
        self.assertEqual(self.fake.page(self.page["id"]), before)

    def test_unknown_fields_fail_with_a_json_error(self) -> None:
        self.assert_fails(
            ["camera", "enrich", self.page["id"]], json.dumps({"Name": "x", "lens": 1}), "unknown fields"
        )

    def test_invalid_values_fail_with_a_json_error(self) -> None:
        payloads: list[object] = [{"name": 5}, {"tags": "Sony"}, {"tags": [1]}, {"wiki": [""]}, {}, []]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assert_fails(["camera", "enrich", self.page["id"]], json.dumps(payload), "")

    def test_malformed_json_fails(self) -> None:
        self.assert_fails(["camera", "enrich", self.page["id"]], "{nope", "not valid JSON")

    def test_a_page_outside_cameras_is_refused(self) -> None:
        self.fake.add_data_source("ds-other")
        other = self.fake.add_page("ds-other", {"Name": title("Not a camera")})
        status, _, err = self.run_command(
            ["camera", "enrich", other["id"]], stdin=json.dumps({"name": "x"})
        )
        self.assertEqual(status, 2)
        self.assertIn("not in the Cameras", json.loads(err)["error"])

    def test_a_missing_page_reports_the_notion_error(self) -> None:
        status, _, err = self.run_command(
            ["camera", "enrich", "nope"], stdin=json.dumps({"name": "x"})
        )
        self.assertEqual(status, 2)
        self.assertIn("object_not_found", json.loads(err)["error"])

    def test_missing_token_and_missing_data_source_fail(self) -> None:
        status, _, err = self.run_command(["camera", "get"], token=None)
        self.assertEqual(status, 2)
        self.assertIn("T6_NOTION_TOKEN", json.loads(err)["error"])
        status, _, err = self.run_command(["camera", "get"], data_sources={"bays": "x"})
        self.assertEqual(status, 2)
        self.assertIn("cameras", json.loads(err)["error"])


if __name__ == "__main__":
    unittest.main()
