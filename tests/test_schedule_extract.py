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


def span_rows(snap: ScheduleSnapshot) -> list[tuple[str, str, str]]:
    return [(c.title, c.start, c.due) for c in snap.candidates]


class SpanTests(unittest.TestCase):
    def test_multi_day_merge_is_one_candidate_with_inclusive_dates(self) -> None:
        tab = TabBuilder()
        tab.add_month(2026, 11, wide={5: (7, "Shoot block")})  # Thursday to Saturday
        snap = snapshot_of(tab.request(year=2026))
        self.assertEqual(span_rows(snap), [("Shoot block", "2026-11-05", "2026-11-07")])
        self.assertEqual(snap.groups[0].status, "complete")
        prov = snap.candidates[0].provenance
        self.assertTrue(prov.merged)
        self.assertEqual((prov.anchor[0], prov.range[0], prov.range.split(":")[1][0]), ("I", "I", "N"))
        self.assertEqual(prov.date_evidence.weekday, "THURSDAY")

    def test_weekend_endpoints(self) -> None:
        tab = TabBuilder()
        tab.add_month(2026, 11, wide={1: (2, "Sunday start"), 6: (7, "Saturday end")})
        snap = snapshot_of(tab.request(year=2026))
        self.assertEqual(
            span_rows(snap),
            [("Sunday start", "2026-11-01", "2026-11-02"), ("Saturday end", "2026-11-06", "2026-11-07")],
        )

    def test_full_week_span(self) -> None:
        tab = TabBuilder()
        tab.add_month(2026, 11, wide={8: (14, "Whole week")})
        self.assertEqual(span_rows(snapshot_of(tab.request(year=2026))), [("Whole week", "2026-11-08", "2026-11-14")])

    def test_taller_merge_still_reads_horizontal_extent_only(self) -> None:
        tab = TabBuilder()
        tab.add_month(2026, 11, wide={9: (11, "Tall block")})
        start = next(m for m in tab.merges if m["end_col"] - m["start_col"] == 6)
        tab.merges.remove(start)
        tab.merges.append({**start, "end_row": start["end_row"] + 2})
        snap = snapshot_of(tab.request(year=2026))
        self.assertEqual(span_rows(snap), [("Tall block", "2026-11-09", "2026-11-11")])
        self.assertEqual(snap.groups[0].status, "complete")

    def test_span_ending_on_a_month_end_and_the_next_month_start(self) -> None:
        tab = TabBuilder()
        tab.add_month(2026, 11, wide={29: (30, "Month end")})
        tab.add_month(2026, 12, wide={1: (3, "Month start")})
        snap = snapshot_of(tab.request(year=2026))
        self.assertEqual(
            span_rows(snap),
            [("Month end", "2026-11-29", "2026-11-30"), ("Month start", "2026-12-01", "2026-12-03")],
        )

    def test_spans_across_a_year_end(self) -> None:
        tab = TabBuilder()
        tab.add_month(2026, 12, wide={29: (31, "Year end")})
        tab.add_month(2027, 1, wide={1: (2, "New year")})
        snap = snapshot_of(tab.request(year=2026))
        self.assertEqual(
            span_rows(snap),
            [("Year end", "2026-12-29", "2026-12-31"), ("New year", "2027-01-01", "2027-01-02")],
        )

    def test_span_running_into_blank_days_is_held_as_ambiguous(self) -> None:
        tab = TabBuilder()
        tab.add_month(2026, 11, {3: ["Grade review"]}, wide={29: (32, "Runs off the month")})
        snap = snapshot_of(tab.request(year=2026))
        [group] = snap.groups
        self.assertEqual(group.status, "held")
        self.assertEqual([e.code for e in group.exceptions], ["ambiguous_span"])
        self.assertIn("Runs off the month", group.exceptions[0].detail)
        self.assertEqual(snap.candidates, [])

    def test_span_ending_mid_pair_is_held_as_ambiguous(self) -> None:
        tab = TabBuilder()
        tab.add_month(2026, 11, wide={9: (11, "Half day end")})
        merge = next(m for m in tab.merges if m["end_col"] - m["start_col"] == 6)
        tab.merges.remove(merge)
        tab.merges.append({**merge, "end_col": merge["end_col"] - 1})
        snap = snapshot_of(tab.request(year=2026))
        self.assertEqual(snap.groups[0].status, "held")
        self.assertEqual(snap.groups[0].exceptions[0].code, "ambiguous_span")
        self.assertEqual(snap.candidates, [])

    def test_span_with_non_consecutive_day_numbers_is_held(self) -> None:
        tab = TabBuilder()
        tab.add_month(2026, 11, wide={9: (11, "Odd numbers")})
        number_row = next(r for (r, c), v in tab.cells.items() if v == 9.0 and c == 2)
        tab.cells[(number_row, 6)] = 18.0  # Wednesday says 18, a Wednesday but not day 11
        snap = snapshot_of(tab.request(year=2026))
        self.assertEqual(snap.groups[0].exceptions[0].code, "ambiguous_span")

    def test_span_hold_leaves_other_groups_reading(self) -> None:
        tab = TabBuilder()
        tab.add_month(2026, 11, wide={29: (32, "Runs off the month")})
        tab.add_month(2026, 12, {1: ["Mix"]})
        snap = snapshot_of(tab.request(year=2026))
        self.assertEqual([g.status for g in snap.groups], ["held", "complete"])
        self.assertEqual([c.title for c in snap.candidates], ["Mix"])

    def test_span_beyond_the_calendar_columns_is_held(self) -> None:
        tab = TabBuilder()
        tab.add_month(2026, 11, wide={8: (14, "Into notes")})
        merge = next(m for m in tab.merges if m["end_col"] - m["start_col"] == 14 and m["start_row"] > 6)
        tab.merges.remove(merge)
        tab.merges.append({**merge, "end_col": 15})
        snap = snapshot_of(tab.request(year=2026))
        self.assertEqual(snap.groups[0].exceptions[0].code, "ambiguous_span")

    def test_span_start_under_a_blank_day_is_undated(self) -> None:
        tab = TabBuilder()
        tab.add_month(2026, 12, {1: ["Grade review"]})  # Sunday and Monday are blank
        number_row = next(r for (r, c), v in tab.cells.items() if v == 1.0 and c == 4)
        tab.cells[(number_row + 2, 0)] = "Orphan span"
        tab.merges.append({"start_row": number_row + 2, "end_row": number_row + 3, "start_col": 0, "end_col": 4})
        snap = snapshot_of(tab.request(year=2026))
        self.assertEqual([c.title for c in snap.candidates], ["Grade review"])
        self.assertEqual(snap.groups[0].exceptions[0].code, "undated_entry")

    def test_each_hold_has_its_own_code(self) -> None:
        header = TabBuilder()
        header.add_month(2026, 11, {3: ["A"]}, weekday_header=False)
        year = TabBuilder()
        year.add_month(2026, 11, {3: ["A"]})
        weekday = TabBuilder()
        weekday.add_month(2026, 11, {3: ["A"]})
        span = TabBuilder()
        span.add_month(2026, 11, wide={29: (32, "Off")})
        codes = [
            snapshot_of(header.request(year=2026)).groups[0].exceptions[0].code,
            snapshot_of(year.request()).groups[0].exceptions[0].code,
            snapshot_of(weekday.request(year=2027)).groups[0].exceptions[0].code,
            snapshot_of(span.request(year=2026)).groups[0].exceptions[0].code,
        ]
        self.assertEqual(codes, ["invalid_header", "ambiguous_year", "weekday_mismatch", "ambiguous_span"])
        for tab, kwargs in ((header, {"year": 2026}), (year, {}), (weekday, {"year": 2027}), (span, {"year": 2026})):
            snap = snapshot_of(tab.request(**kwargs))
            self.assertEqual(snap.candidates, [])
            self.assertTrue(snap.groups[0].exceptions[0].detail)


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
