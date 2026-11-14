import unittest

from t6.notion.package_specs import ChecklistItem, PackageSpecReader
from t6.notion.testing import FakeNotion, package_spec_properties


class PackageSpecReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeNotion()
        self.fake.add_data_source("specs")
        self.general = self.fake.add_page("specs", package_spec_properties(
            package_kind="Color", version="1", advisory_items=("Picture lock", "Reference"),
        ))
        self.client = self.fake.add_page("specs", package_spec_properties(
            package_kind="Color", client_spec="Studio A", version="2.1",
            advisory_items=("Reference",), blocking_items=("  EDL  ",),
        ))
        self.fake.add_page("specs", package_spec_properties(package_kind="Sound", version="1"))
        self.reader = PackageSpecReader(self.fake.client(), "specs")

    def test_general_spec_is_all_advisory(self) -> None:
        spec, = self.reader.find("Color")
        self.assertEqual((spec.page_id, spec.package_kind, spec.client_spec, spec.version),
                         (self.general["id"], "Color", None, "1"))
        self.assertEqual(spec.items, (ChecklistItem("Picture lock"), ChecklistItem("Reference")))

    def test_client_spec_and_blocking_items(self) -> None:
        spec, = self.reader.find("Color", "Studio A")
        self.assertEqual((spec.page_id, spec.version), (self.client["id"], "2.1"))
        self.assertEqual(spec.items, (ChecklistItem("Reference"), ChecklistItem("EDL", True)))
        self.assertEqual(self.reader.find("Color", "other"), ())
        self.assertTrue(all(request.method == "POST" and request.url.path.endswith("/query")
                            for request in self.fake.requests))

    def test_archived_and_duplicate_rows(self) -> None:
        self.fake.add_page("specs", package_spec_properties(package_kind="Color", version="3"))
        archived = self.fake.add_page("specs", package_spec_properties(package_kind="Color", version="4"))
        self.fake._pages[archived["id"]]["archived"] = True
        self.assertEqual([spec.version for spec in self.reader.find("Color")], ["1", "3"])

    def test_missing_version_is_not_silently_used(self) -> None:
        self.fake.add_page("specs", package_spec_properties(package_kind="Sound", version=""))
        with self.assertRaisesRegex(ValueError, "has no version"):
            self.reader.find("Sound")


if __name__ == "__main__":
    unittest.main()
