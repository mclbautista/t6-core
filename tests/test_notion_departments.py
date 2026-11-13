"""Departments come from live Notion options, by ID, in studio display order."""

import unittest
from unittest import mock

from t6.notion.departments import (
    Department,
    UnknownDepartment,
    display_rank,
    get_departments,
    resolve_department,
)
from t6.notion.generated import JobOrdersRow
from t6.notion.testing import FakeNotion

SOURCE = "ds-job-orders"
NAMES = ["VFX", "Misc", "SOUND", "TOC", "Zebra", "AE", "ADMIN", "Apple", "OE", "COLOR", "COOR", "MISC"]


def live_job_orders(names: list[str] = NAMES) -> FakeNotion:
    fake = FakeNotion()
    source = fake.add_snapshot_data_source(SOURCE, "job_orders")
    fake._data_sources[SOURCE]["properties"]["Department"]["select"] = {
        "options": [{"id": f"id-{name}", "name": name} for name in names]
    }
    del source
    return fake


class DepartmentTests(unittest.TestCase):
    def test_options_come_back_in_display_order_with_ids(self) -> None:
        with live_job_orders().client() as client:
            departments = get_departments(client, SOURCE)
        self.assertEqual(
            [d.name for d in departments],
            ["TOC", "AE", "OE", "COOR", "COLOR", "VFX", "SOUND", "ADMIN", "Apple", "Zebra"],
        )
        self.assertEqual(departments[0], Department("id-TOC", "TOC"))

    def test_misc_is_not_a_department(self) -> None:
        with live_job_orders(["MISC", "AE"]).client() as client:
            self.assertEqual([d.name for d in get_departments(client, SOURCE)], ["AE"])

    def test_a_renamed_property_or_option_is_still_read_by_id(self) -> None:
        # The committed snapshot has no real IDs yet, so give the property one.
        fake = live_job_orders(["TOC", "AE"])
        fake._data_sources[SOURCE]["properties"]["Department"]["id"] = "dep1"
        fake.rename_property(SOURCE, "Department", "Dept")
        fake.rename_option(SOURCE, "Dept", "AE", "Audio Edit")
        with mock.patch.object(JobOrdersRow, "notion_id", return_value="dep1"), fake.client() as client:
            departments = get_departments(client, SOURCE)
        self.assertEqual([(d.option_id, d.name) for d in departments], [("id-TOC", "TOC"), ("id-AE", "Audio Edit")])

    def test_without_a_snapshot_id_the_property_is_found_by_name(self) -> None:
        with live_job_orders(["TOC"]).client() as client:
            self.assertEqual([d.name for d in get_departments(client, SOURCE)], ["TOC"])

    def test_aliases_and_case_resolve(self) -> None:
        with live_job_orders().client() as client:
            departments = get_departments(client, SOURCE)
        self.assertEqual(resolve_department(departments, "to"), Department("id-TOC", "TOC"))
        self.assertEqual(resolve_department(departments, " Sound "), Department("id-SOUND", "SOUND"))

    def test_an_unknown_token_is_typed_not_guessed(self) -> None:
        with live_job_orders().client() as client:
            departments = get_departments(client, SOURCE)
        self.assertEqual(resolve_department(departments, "TOCC"), UnknownDepartment("TOCC"))
        self.assertEqual(resolve_department(departments, "MISC"), UnknownDepartment("MISC"))

    def test_an_alias_whose_target_is_not_live_is_unknown(self) -> None:
        with live_job_orders(["AE"]).client() as client:
            departments = get_departments(client, SOURCE)
        self.assertEqual(resolve_department(departments, "TO"), UnknownDepartment("TO"))

    def test_no_department_sorts_last(self) -> None:
        names = ["Zebra", None, "AE", "TOC"]
        self.assertEqual(sorted(names, key=display_rank), ["TOC", "AE", "Zebra", None])


if __name__ == "__main__":
    unittest.main()
