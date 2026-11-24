"""Read coverage and group status (#719): held is not incomplete, and missing is never "no task"."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from schedule_fixtures import TabBuilder

from t6.schedule import ScheduleSnapshot, UnsupportedLayout, extract_schedule, parse_request, parse_response
from t6.schedule.contract import ScheduleRequest, dump_response, round_trip
from t6.schedule.coverage import standing


def snapshot_of(request: ScheduleRequest) -> ScheduleSnapshot:
    result = extract_schedule(request)
    assert isinstance(result, ScheduleSnapshot), result
    round_trip(result)
    return result


def three_months(**kwargs: Any) -> ScheduleRequest:
    """November to January, across a year end, with a task in each month."""
    tab = TabBuilder()
    tab.add_month(2026, 11, {3: ["Grade review"]})
    tab.add_month(2026, 12, {1: ["Mix"], 2: ["Mix"]})
    tab.add_month(2027, 1, {4: ["Deliver"]})
    return tab.request(year=2026, **kwargs)


def first_chunk(request: ScheduleRequest, rows: int | None = None, columns: int | None = None) -> ScheduleRequest:
    """The same tab as a short read: only the first rows and columns, with the tab's real size still reported."""
    rows = rows if rows is not None else len(request.cells)
    columns = columns if columns is not None else request.columns_total
    cells = [row[:columns] for row in request.cells[:rows]]
    return request.model_copy(update={"cells": cells})


class VersionedFixtureTests(unittest.TestCase):
    """The held-group and truncated-read fixtures: both are committed as request and response pairs."""

    def result(self, name: str) -> ScheduleSnapshot:
        fixtures = Path(__file__).parent / "fixtures" / "schedule"
        request = json.loads((fixtures / f"request.{name}.v1.json").read_text())
        expected = json.loads((fixtures / f"response.{name}.v1.json").read_text())
        result = extract_schedule(parse_request(request))
        self.assertEqual(dump_response(result), expected)
        parsed = parse_response(expected)
        assert isinstance(parsed, ScheduleSnapshot)
        self.assertEqual(round_trip(parsed), parsed)
        return parsed

    def test_held_group_fixture(self) -> None:
        snap = self.result("held")
        self.assertEqual(snap.coverage.status, "complete")
        self.assertEqual([g.status for g in snap.groups], ["complete", "held"])

    def test_truncated_read_fixture(self) -> None:
        snap = self.result("truncated")
        self.assertEqual(snap.coverage.status, "incomplete")
        self.assertEqual(snap.coverage.reasons, ["rows_short"])
        self.assertNotIn("complete", [g.status for g in snap.groups])


class FullCoverageTests(unittest.TestCase):
    def test_a_full_read_states_ranges_and_every_group_is_complete(self) -> None:
        request = three_months()
        snap = snapshot_of(request)
        cov = snap.coverage
        self.assertEqual(cov.status, "complete")
        self.assertEqual(cov.reasons, [])
        self.assertEqual(cov.read_range, f"A1:O{request.rows_total}")
        self.assertEqual(cov.read_range, cov.total_range)
        self.assertEqual([g.status for g in snap.groups], ["complete"] * 3)
        self.assertEqual([c.start for c in snap.candidates], ["2026-11-03", "2026-12-01", "2026-12-02", "2027-01-04"])

    def test_end_to_end_through_json(self) -> None:
        wire = json.dumps(three_months().model_dump(mode="json"))
        result = extract_schedule(parse_request(wire))
        assert isinstance(result, ScheduleSnapshot)
        self.assertEqual(dump_response(round_trip(result)), dump_response(result))
        self.assertEqual(result.coverage.status, "complete")
        self.assertEqual(len(result.groups), 3)

    def test_days_are_read_or_blank_and_a_day_with_no_task_is_read(self) -> None:
        tab = TabBuilder()
        tab.add_month(2026, 12, {1: ["Mix"]})  # December 2026 starts on a Tuesday: Sunday and Monday are blank
        [group] = snapshot_of(tab.request(year=2026)).groups
        by_state: dict[str, int] = {}
        for day in group.days:
            by_state[day.state] = by_state.get(day.state, 0) + 1
        self.assertEqual(by_state, {"read": 31, "blank": by_state["blank"]})
        self.assertGreater(by_state["blank"], 0)
        blank = next(d for d in group.days if d.state == "blank")
        self.assertIsNone(blank.day)
        self.assertIsNone(blank.date)
        self.assertEqual(next(d for d in group.days if d.day == 9).date, "2026-12-09")


class TruncatedReadTests(unittest.TestCase):
    def test_first_chunk_of_rows_is_incomplete_not_a_complete_snapshot_with_fewer_candidates(self) -> None:
        full = three_months()
        snap = snapshot_of(first_chunk(full, rows=20))
        cov = snap.coverage
        self.assertEqual(cov.status, "incomplete")
        self.assertEqual(cov.reasons, ["rows_short"])
        self.assertEqual((cov.rows_read, cov.rows_total), (20, full.rows_total))
        self.assertEqual(cov.read_range, "A1:O20")
        self.assertEqual(cov.total_range, f"A1:O{full.rows_total}")
        self.assertTrue(snap.groups)
        for group in snap.groups:
            self.assertEqual(group.status, "incomplete")
            self.assertEqual([e.code for e in group.exceptions], ["unread_region"])
            self.assertNotIn("read", {d.state for d in group.days})
            self.assertEqual(standing(snap, group.group_key), "incomplete")

    def test_columns_cut_short_are_incomplete(self) -> None:
        snap = snapshot_of(first_chunk(three_months(), columns=10))
        self.assertEqual(snap.coverage.reasons, ["columns_short"])
        self.assertEqual(snap.coverage.read_range[:4], "A1:J")  # type: ignore[index]
        self.assertNotIn("complete", [g.status for g in snap.groups])  # a cut header may read as held, never complete

    def test_unreported_size_is_incomplete(self) -> None:
        request = three_months().model_copy(update={"rows_total": None, "columns_total": None})
        snap = snapshot_of(request)
        self.assertEqual(snap.coverage.reasons, ["extent_unknown"])
        self.assertIsNone(snap.coverage.total_range)
        self.assertTrue(all(g.status == "incomplete" for g in snap.groups))

    def test_missing_merge_list_is_incomplete_and_reads_no_candidates(self) -> None:
        request = three_months().model_copy(update={"merges": None})
        snap = snapshot_of(request)
        self.assertEqual(snap.coverage.status, "incomplete")
        self.assertEqual(snap.coverage.reasons, ["merges_missing"])
        self.assertEqual(snap.candidates, [])
        self.assertEqual(snap.groups, [])

    def test_an_explicit_empty_merge_list_is_a_tab_without_merges_not_a_missing_list(self) -> None:
        request = three_months().model_copy(update={"merges": []})
        self.assertIsInstance(extract_schedule(request), UnsupportedLayout)

    def test_a_held_group_stays_held_in_an_incomplete_source(self) -> None:
        tab = TabBuilder()
        tab.add_month(2026, 11, {3: ["Grade review"]}, weekday_header=False)
        tab.add_month(2026, 12, {1: ["Mix"]})
        snap = snapshot_of(first_chunk(tab.request(year=2026), rows=60).model_copy(update={"rows_total": 80}))
        self.assertEqual([g.status for g in snap.groups], ["held", "incomplete"])


class HeldIsNotIncompleteTests(unittest.TestCase):
    def test_a_problem_in_one_group_holds_it_and_leaves_the_source_complete(self) -> None:
        tab = TabBuilder()
        tab.add_month(2026, 11, {3: ["Grade review"]})
        tab.add_month(2026, 12, {1: ["Mix"]}, weekday_header=False)
        snap = snapshot_of(tab.request(year=2026))
        self.assertEqual(snap.coverage.status, "complete")
        self.assertEqual([g.status for g in snap.groups], ["complete", "held"])

    def test_the_same_problem_in_the_source_leaves_no_group_complete(self) -> None:
        snap = snapshot_of(first_chunk(three_months(), rows=20))
        self.assertEqual(snap.coverage.status, "incomplete")
        self.assertNotIn("complete", [g.status for g in snap.groups])
        self.assertNotIn("held", [g.status for g in snap.groups])


class AbsenceTests(unittest.TestCase):
    """A candidate in revision A that is missing from revision B is only missing if B read its group."""

    def revision_a(self) -> ScheduleSnapshot:
        tab = TabBuilder()
        tab.add_month(2026, 11, {3: ["Grade review"]})
        tab.add_month(2026, 12, {1: ["Mix"]})
        return snapshot_of(tab.request(year=2026))

    def test_candidate_inside_a_held_group_in_b_is_held_not_missing(self) -> None:
        a = self.revision_a()
        mix = next(c for c in a.candidates if c.title == "Mix")

        tab = TabBuilder()
        tab.add_month(2026, 11, {3: ["Grade review"]})
        tab.add_month(2026, 12, {1: ["Mix"]}, weekday_header=False)
        b = snapshot_of(tab.request(year=2026))

        self.assertNotIn("Mix", [c.title for c in b.candidates])
        self.assertEqual(standing(b, mix.group_key), "held")
        self.assertEqual(standing(b, mix.group_key, mix.start), "held")
        self.assertEqual(standing(b, "m01-nov"), "read")

    def test_candidate_in_a_group_held_by_an_ambiguous_span_is_held_not_missing(self) -> None:
        a = self.revision_a()
        grade = next(c for c in a.candidates if c.title == "Grade review")

        tab = TabBuilder()
        tab.add_month(2026, 11, {}, wide={29: (32, "Runs off the month")})
        tab.add_month(2026, 12, {1: ["Mix"]})
        b = snapshot_of(tab.request(year=2026))

        self.assertNotIn("Grade review", [c.title for c in b.candidates])
        self.assertEqual(standing(b, grade.group_key), "held")
        self.assertEqual(standing(b, grade.group_key, "2026-11-03"), "held")

    def test_omitted_cell_in_a_complete_group_is_no_task_only_when_the_day_was_read(self) -> None:
        a = self.revision_a()
        grade = next(c for c in a.candidates if c.title == "Grade review")

        tab = TabBuilder()
        tab.add_month(2026, 11, {})
        tab.add_month(2026, 12, {1: ["Mix"]})
        b = snapshot_of(tab.request(year=2026))

        self.assertEqual(standing(b, grade.group_key, grade.start), "read")

    def test_candidates_of_a_truncated_read_are_never_proof_of_absence(self) -> None:
        a = self.revision_a()
        mix = next(c for c in a.candidates if c.title == "Mix")
        short = snapshot_of(first_chunk(self.request_b(), rows=18))
        self.assertNotIn("Mix", [c.title for c in short.candidates])
        self.assertEqual(standing(short, mix.group_key), "incomplete")
        self.assertEqual(standing(short, "m02-dec"), "incomplete")  # the group was never reached

    def request_b(self) -> ScheduleRequest:
        tab = TabBuilder()
        tab.add_month(2026, 11, {3: ["Grade review"]})
        tab.add_month(2026, 12, {1: ["Mix"]})
        return tab.request(year=2026)

    def test_group_missing_from_a_complete_snapshot_is_not_a_deletion_either(self) -> None:
        tab = TabBuilder()
        tab.add_month(2026, 11, {3: ["Grade review"]})
        b = snapshot_of(tab.request(year=2026))
        self.assertEqual(standing(b, "m02-dec"), "not_in_snapshot")


class DayStateTests(unittest.TestCase):
    def test_invalid_day_number_is_invalid_and_holds_the_group(self) -> None:
        tab = TabBuilder()
        top = tab.add_month(2026, 11, {3: ["Grade review"]})
        number_row = next(r for (r, c), v in tab.cells.items() if v == 3.0 and r > top)
        tab.cells[(number_row, 4)] = 99.0
        [group] = snapshot_of(tab.request(year=2026)).groups
        self.assertEqual(group.status, "held")
        invalid = [d for d in group.days if d.state == "invalid"]
        self.assertEqual([d.anchor for d in invalid], [f"E{number_row + 1}"])
        self.assertIsNone(invalid[0].date)

    def test_non_numeric_text_in_a_day_number_row_is_invalid_not_blank(self) -> None:
        tab = TabBuilder()
        top = tab.add_month(2026, 11, {3: ["Grade review"]})
        number_row = next(r for (r, c), v in tab.cells.items() if v == 3.0 and r > top)
        tab.cells[(number_row, 4)] = "tbd"
        [group] = snapshot_of(tab.request(year=2026)).groups
        self.assertEqual(group.status, "held")
        self.assertEqual([e.code for e in group.exceptions], ["invalid_day_number"])
        self.assertIn("invalid", {d.state for d in group.days})

    def test_a_held_year_leaves_numbered_days_unread(self) -> None:
        tab = TabBuilder()
        tab.add_month(2026, 11, {3: ["Grade review"]})
        [group] = snapshot_of(tab.request()).groups  # no year: ambiguous
        self.assertEqual(group.status, "held")
        self.assertEqual({d.state for d in group.days}, {"unread", "blank"})
        self.assertTrue(all(d.date is None for d in group.days))

    def test_days_under_an_ambiguous_span_are_unread(self) -> None:
        tab = TabBuilder()
        tab.add_month(2026, 11, {}, wide={29: (32, "Runs off the month")})
        [group] = snapshot_of(tab.request(year=2026)).groups
        unread = sorted(d.date for d in group.days if d.state == "unread")  # type: ignore[type-var]
        self.assertEqual(unread, ["2026-11-29", "2026-11-30"])


if __name__ == "__main__":
    unittest.main()
