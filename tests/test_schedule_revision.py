from __future__ import annotations

import unittest

from t6.schedule import ScheduleSnapshot, extract_schedule
from t6.schedule.contract import ScheduleRequest, round_trip
from schedule_fixtures import TabBuilder


def snap(request: ScheduleRequest) -> ScheduleSnapshot:
    result = extract_schedule(request)
    assert isinstance(result, ScheduleSnapshot), result
    round_trip(result)
    return result


def tab(entries: dict[int, list[str]] | None = None) -> TabBuilder:
    builder = TabBuilder()
    builder.add_month(2026, 11, entries if entries is not None else {3: ["Grade review"], 10: ["Mix"]})
    return builder


def cell_of(builder: TabBuilder, text: str) -> tuple[int, int]:
    return next(rc for rc, v in builder.cells.items() if v == text)


class SourceRevisionTests(unittest.TestCase):
    def test_google_revision_is_used_when_the_request_gives_one(self) -> None:
        result = snap(tab().request(year=2026, source_revision="rev-42"))
        self.assertEqual(result.source_revision, "rev-42")

    def test_fingerprint_is_stable_for_identical_input(self) -> None:
        first = snap(tab().request(year=2026)).source_revision
        second = snap(tab().request(year=2026)).source_revision
        self.assertIsNotNone(first)
        self.assertTrue((first or "").startswith("sha256:"))
        self.assertEqual(first, second)

    def test_fingerprint_ignores_trailing_whitespace_in_cell_text(self) -> None:
        plain = snap(tab().request(year=2026)).source_revision
        noisy = tab()
        noisy.cells[cell_of(noisy, "Mix")] = "Mix \t\n"
        self.assertEqual(snap(noisy.request(year=2026)).source_revision, plain)

    def test_fingerprint_changes_when_a_value_changes(self) -> None:
        base = snap(tab().request(year=2026)).source_revision
        changed = snap(tab({3: ["Grade review"], 10: ["Mix v2"]}).request(year=2026)).source_revision
        self.assertNotEqual(base, changed)

    def test_fingerprint_changes_when_a_merge_changes(self) -> None:
        base = snap(tab().request(year=2026)).source_revision
        builder = tab()
        row, col = cell_of(builder, "Mix")
        merge = next(m for m in builder.merges if (m["start_row"], m["start_col"]) == (row, col))
        merge["end_col"] += 2
        self.assertNotEqual(snap(builder.request(year=2026)).source_revision, base)

    def test_unsupported_tab_carries_a_stable_fingerprint(self) -> None:
        request = ScheduleRequest.model_validate(
            {"source_scope": {"document_id": "d", "tab_id": "t"}, "cells": [["Task", "Start"]]}
        )
        first, second = extract_schedule(request), extract_schedule(request)
        self.assertTrue((first.source_revision or "").startswith("sha256:"))
        self.assertEqual(first.source_revision, second.source_revision)


class MovedCellTests(unittest.TestCase):
    def moved_pair(self, with_id: bool) -> tuple[ScheduleSnapshot, ScheduleSnapshot]:
        """The entry moves from Tuesday the 3rd to Thursday the 5th between two reads."""
        old, new = tab({3: ["Grade review"]}), tab({5: ["Grade review"]})
        requests = []
        for builder in (old, new):
            row, col = cell_of(builder, "Grade review")
            ids = [{"row": row, "col": col, "id": "src-7"}] if with_id else []
            requests.append(builder.request(year=2026, source_ids=ids))
        return snap(requests[0]), snap(requests[1])

    def test_moved_cell_keeps_the_supplied_id_and_takes_new_provenance(self) -> None:
        old, new = self.moved_pair(with_id=True)
        [before], [after] = old.candidates, new.candidates
        self.assertEqual((before.source_id, after.source_id), ("src-7", "src-7"))
        self.assertEqual((before.start, after.start), ("2026-11-03", "2026-11-05"))
        self.assertNotEqual(before.provenance.anchor, after.provenance.anchor)
        self.assertNotEqual(old.source_revision, new.source_revision)

    def test_without_an_id_the_snapshot_claims_no_link(self) -> None:
        old, new = self.moved_pair(with_id=False)
        [before], [after] = old.candidates, new.candidates
        self.assertIsNone(before.source_id)
        self.assertIsNone(after.source_id)
        self.assertNotEqual(before.provenance.anchor, after.provenance.anchor)

    def test_an_id_on_a_cell_that_is_not_a_task_is_attached_to_no_candidate(self) -> None:
        request = tab({3: ["Grade review"]}).request(year=2026, source_ids=[{"row": 0, "col": 0, "id": "stray"}])
        self.assertIsNone(snap(request).candidates[0].source_id)


if __name__ == "__main__":
    unittest.main()
