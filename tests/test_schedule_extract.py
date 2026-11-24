from __future__ import annotations

import unittest

from t6.schedule import ScheduleSnapshot, UnsupportedLayout, extract_schedule
from t6.schedule.contract import ScheduleRequest, round_trip
from schedule_fixtures import TabBuilder


def snapshot_of(request: ScheduleRequest) -> ScheduleSnapshot:
    result = extract_schedule(request)
    assert isinstance(result, ScheduleSnapshot), result
    round_trip(result)
    return result


class SingleDayEntryTests(unittest.TestCase):
    def test_single_day_cells_become_candidates_with_provenance(self) -> None:
        tab = TabBuilder()
        top = tab.add_month(2026, 11, {3: ["Grade review"], 10: ["2ND pass\t\n"], 12: ["Grade review"]})
        snap = snapshot_of(tab.request(year=2026))

        self.assertEqual(snap.coverage.status, "complete")
        [group] = snap.groups
        self.assertEqual(group.status, "complete")
        self.assertEqual(group.year, 2026)
        self.assertEqual(
            [(c.title, c.start, c.due) for c in snap.candidates],
            [
                ("Grade review", "2026-11-03", "2026-11-03"),
                ("2ND pass", "2026-11-10", "2026-11-10"),
                ("Grade review", "2026-11-12", "2026-11-12"),
            ],
        )
        self.assertEqual(len({c.candidate_key for c in snap.candidates}), 3)
        first = snap.candidates[0]
        self.assertEqual(first.group_key, group.group_key)
        self.assertIsNone(first.tentative)
        self.assertIsNone(first.source_id)
        prov = first.provenance
        self.assertEqual((prov.tab_id, prov.tab_title), ("tab-1", "as of Nov 1"))
        self.assertTrue(prov.merged)
        self.assertEqual(prov.anchor[0], "E")  # Tuesday is the E:F pair
        self.assertEqual(prov.range[0], "E")
        self.assertEqual(prov.year_evidence.year, 2026)
        self.assertEqual(prov.year_evidence.method, "supplied")
        self.assertEqual((prov.date_evidence.month, prov.date_evidence.day), (11, 3))
        self.assertEqual(prov.date_evidence.weekday, "TUESDAY")
        self.assertEqual(prov.date_evidence.month_header_anchor, f"A{top + 1}")

    def test_unmerged_cell_and_fill_colour_are_kept(self) -> None:
        tab = TabBuilder()
        tab.add_month(2026, 11, {4: ["Offline edit"]})
        day_row = next(r for (r, c), v in tab.cells.items() if v == "Offline edit")
        tab.merges = [m for m in tab.merges if not (m["start_row"] == day_row and m["end_row"] - m["start_row"] == 1 and m["end_col"] - m["start_col"] == 2 and m["start_col"] == 6)]
        tab.fills.append({"row": day_row, "col": 6, "color": "#ffcc00"})
        [candidate] = snapshot_of(tab.request(year=2026)).candidates
        self.assertFalse(candidate.provenance.merged)
        self.assertEqual(candidate.provenance.fill, "#ffcc00")
        self.assertEqual(candidate.provenance.range, candidate.provenance.anchor)

    def test_availability_is_not_a_task(self) -> None:
        tab = TabBuilder()
        tab.add_month(2026, 11, {5: ["NA: Person A", "Grade review"]})
        snap = snapshot_of(tab.request(year=2026))
        self.assertEqual([c.title for c in snap.candidates], ["Grade review"])
        [item] = snap.non_tasks
        self.assertEqual((item.kind, item.text), ("availability", "NA: Person A"))

    def test_months_across_a_year_end_roll_the_year_over(self) -> None:
        tab = TabBuilder()
        tab.add_month(2026, 12, {30: ["Year end"]})
        tab.add_month(2027, 1, {4: ["New year"]})
        snap = snapshot_of(tab.request(year=2026))
        self.assertEqual(
            [(c.title, c.start) for c in snap.candidates],
            [("Year end", "2026-12-30"), ("New year", "2027-01-04")],
        )
        self.assertEqual([g.year for g in snap.groups], [2026, 2027])
        self.assertEqual(len({g.group_key for g in snap.groups}), 2)


class YearTests(unittest.TestCase):
    def test_year_is_inferred_when_exactly_one_year_fits(self) -> None:
        tab = TabBuilder()
        tab.add_month(2028, 1, {5: ["Grade review"]})
        tab.add_month(2028, 3, {6: ["Mix"]})
        snap = snapshot_of(tab.request())
        self.assertEqual([c.start for c in snap.candidates], ["2028-01-05", "2028-03-06"])
        self.assertEqual(snap.candidates[0].provenance.year_evidence.method, "inferred_weekday_alignment")

    def test_ambiguous_year_holds_every_group_without_candidates(self) -> None:
        tab = TabBuilder()
        tab.add_month(2026, 11, {3: ["Grade review"]})
        snap = snapshot_of(tab.request())
        self.assertEqual(snap.candidates, [])
        [group] = snap.groups
        self.assertEqual(group.status, "held")
        self.assertEqual([e.code for e in group.exceptions], ["ambiguous_year"])
        self.assertIsNone(group.year)

    def test_supplied_year_that_breaks_weekday_alignment_holds_the_group(self) -> None:
        tab = TabBuilder()
        tab.add_month(2026, 11, {3: ["Grade review"]})
        snap = snapshot_of(tab.request(year=2027))
        self.assertEqual(snap.candidates, [])
        self.assertEqual(snap.groups[0].status, "held")
        self.assertEqual(snap.groups[0].exceptions[0].code, "weekday_mismatch")


class HoldTests(unittest.TestCase):
    def test_bad_weekday_header_holds_only_that_group(self) -> None:
        tab = TabBuilder()
        tab.add_month(2026, 11, {3: ["Grade review"]}, weekday_header=False)
        tab.add_month(2026, 12, {1: ["Mix"]})
        snap = snapshot_of(tab.request(year=2026))
        self.assertEqual([g.status for g in snap.groups], ["held", "complete"])
        self.assertEqual(snap.groups[0].exceptions[0].code, "invalid_header")
        self.assertEqual([c.title for c in snap.candidates], ["Mix"])

    def test_multi_day_merge_is_not_read_and_marks_the_group_incomplete(self) -> None:
        tab = TabBuilder()
        tab.add_month(2026, 11, {3: ["Grade review"]}, wide={5: (7, "Shoot block")})
        snap = snapshot_of(tab.request(year=2026))
        self.assertEqual([c.title for c in snap.candidates], ["Grade review"])
        [group] = snap.groups
        self.assertEqual(group.status, "incomplete")
        self.assertEqual(group.exceptions[0].code, "span_not_read")

    def test_short_read_makes_the_whole_snapshot_incomplete(self) -> None:
        tab = TabBuilder()
        tab.add_month(2026, 11, {3: ["Grade review"]})
        request = tab.request(year=2026)
        self.assertIsNotNone(request.rows_total)
        snap = snapshot_of(request.model_copy(update={"rows_total": (request.rows_total or 0) + 40}))
        self.assertEqual(snap.coverage.status, "incomplete")

    def test_entry_under_a_blank_day_is_reported_not_dropped(self) -> None:
        tab = TabBuilder()
        tab.add_month(2026, 12, {1: ["Grade review"]})  # December 2026 starts on a Tuesday
        number_row = next(r for (r, c), v in tab.cells.items() if v == 1.0 and c == 4)
        tab.cells[(number_row + 1, 0)] = "Orphan"  # under the blank Sunday
        snap = snapshot_of(tab.request(year=2026))
        self.assertEqual([c.title for c in snap.candidates], ["Grade review"])
        self.assertEqual(snap.groups[0].status, "incomplete")
        self.assertEqual([e.code for e in snap.groups[0].exceptions], ["undated_entry"])


class UnsupportedLayoutTests(unittest.TestCase):
    def test_task_table_is_unsupported_with_a_diagnostic(self) -> None:
        request = ScheduleRequest.model_validate(
            {
                "source_scope": {"document_id": "doc-synthetic", "tab_id": "tab-2"},
                "cells": [["Task", "Start", "End"], ["Grade review", "2026-11-03", "2026-11-04"]],
                "merges": [],
            }
        )
        result = extract_schedule(request)
        assert isinstance(result, UnsupportedLayout)
        self.assertEqual(result.diagnostic.code, "no_month_header")
        self.assertIn("month", result.diagnostic.message)
        self.assertEqual(result.source_scope.tab_id, "tab-2")
        round_trip(result)

    def test_empty_tab_is_unsupported(self) -> None:
        request = ScheduleRequest.model_validate(
            {"source_scope": {"document_id": "d", "tab_id": "t"}, "cells": []}
        )
        result = extract_schedule(request)
        assert isinstance(result, UnsupportedLayout)
        self.assertEqual(result.diagnostic.code, "empty_tab")


if __name__ == "__main__":
    unittest.main()
