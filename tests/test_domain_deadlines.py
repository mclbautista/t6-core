"""Deadline resolution, ported from the JO watchdog's tests as contract tests."""

import unittest
from collections.abc import Sequence
from datetime import datetime, time

from t6.domain import Contact
from t6.domain.deadlines import deadline as resolve_deadline
from t6.domain.deadlines import DeadlineResult, strip_hedges
from t6.domain.studio_time import MANILA


def person(name: str, end_of_shift: time | None) -> Contact:
    return Contact(page_id=name.lower(), name=name, end_of_shift=end_of_shift)


DAY = [person("Lena", time(19))]
NIGHT = [person("Paul", time(5))]
FLEX = [person("Che", None)]


def at(month: int, day: int, hour: int, minute: int = 0, year: int = 2026) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=MANILA)


def codes(result: DeadlineResult) -> list[str]:
    return [f.code for f in result.findings]


class DeadlineTableTests(unittest.TestCase):
    """One case per row of the corpus deadline table (P = posting time)."""

    def check(
        self,
        raw: str | None,
        posted: datetime,
        expected: datetime | None,
        findings: Sequence[str] = (),
        shifts: Sequence[Contact] = DAY,
        source: str | None = None,
    ) -> DeadlineResult:
        result = resolve_deadline(raw, posted, shifts)
        self.assertEqual(result.deadline, expected, raw)
        self.assertEqual(codes(result), list(findings), raw)
        if source:
            self.assertEqual(result.source, source, raw)
        return result

    def test_date_and_time_rows(self) -> None:
        p = at(9, 1, 10)
        self.check("September 29 / Tues 5AM", p, at(9, 29, 5))
        self.check("September 9, 8PM (if keri)", p, at(9, 9, 20))
        self.check("September 15, 530PM", p, at(9, 15, 17, 30))
        self.check("September 23 / Wednesday 12:00 AM", p, at(9, 23, 0))
        self.check("September 27 / Sunday 5AM (pickup D16 shoot)", p, at(9, 27, 5))

    def test_hedged_morning_time(self) -> None:
        self.check("August 17, 11AM if keri", at(8, 17, 9), at(8, 17, 11))

    def test_date_only_rows(self) -> None:
        p = at(9, 1, 10)
        for raw, day in [("Sep 26", 26), ("Sep 8 / Tue", 8), ("Wednesday September 23", 23)]:
            self.check(raw, p, at(9, day, 23, 59), source="stated")

    def test_date_with_parenthetical_containing_tonight(self) -> None:
        self.check(
            "August 20, Thurs (papadala ko tonight ke Che yung HD nya)",
            at(8, 20, 9),
            at(8, 20, 23, 59),
        )

    def test_date_with_no_clock_phrase(self) -> None:
        self.check("August 27 / Thurs / before photoshoot", at(8, 27, 9), at(8, 27, 23, 59))

    def test_date_with_bare_pm(self) -> None:
        self.check("September 11, pm", at(9, 11, 9), at(9, 11, 23, 59))

    def test_date_within_the_day(self) -> None:
        p = at(9, 23, 10)
        self.check("Sept 25 / within the day", p, at(9, 25, 23, 59))
        self.check("Sept 28 / Mon / within the day", p, at(9, 28, 23, 59))

    def test_time_only_uses_posting_day(self) -> None:
        self.check("1pm", at(9, 22, 10), at(9, 22, 13))

    def test_time_only_before_posting_is_kept_and_marked(self) -> None:
        self.check("1pm", at(9, 22, 14), at(9, 22, 13), ["deadline_past"])

    def test_tonight_and_today_rows(self) -> None:
        p = at(9, 21, 10)
        for raw in ["Tonight", "Tonight if keri or tom", "If keri rin po today"]:
            self.check(raw, p, at(9, 21, 23, 59))

    def test_shift_phrase_rows_resolve_to_shift(self) -> None:
        p = at(9, 21, 10)
        for raw in [
            "end of shift",
            "within shift",
            "within shift if kaya",
            "end of shift if kaya, if not tomorrow na po",
        ]:
            self.check(raw, p, at(9, 21, 19), source="shift")

    def test_shift_phrase_rows_fall_back_to_2359(self) -> None:
        p = at(9, 21, 10)
        for raw in ["end of shift", "within shift if kaya"]:
            result = self.check(raw, p, at(9, 21, 23, 59), ["shift_fallback"], shifts=FLEX)
            self.assertEqual(result.source, "fallback")
            self.assertEqual(result.findings[0].params, {"defaulted": ["Che"]})

    def test_date_plus_shift_rows(self) -> None:
        p = at(8, 26, 9)
        self.check("Aug 26, end of shift", p, at(8, 26, 19))
        self.check("Sept 23 / end of shift", at(9, 23, 9), at(9, 23, 19))
        self.check("September 2, within shift", at(9, 2, 9), at(9, 2, 19))

    def test_date_plus_morning_shift_ends_next_day(self) -> None:
        self.check("Aug 26, end of shift", at(8, 26, 9), at(8, 27, 5), shifts=NIGHT)

    def test_date_plus_shift_falls_back_to_that_date(self) -> None:
        self.check(
            "Aug 26, end of shift", at(8, 25, 9), at(8, 26, 23, 59), ["shift_fallback"], shifts=()
        )

    def test_asap_with_date(self) -> None:
        self.check("September 10, asap", at(9, 10, 9), at(9, 10, 19), ["asap"])
        self.check(
            "September 10, asap", at(9, 10, 9), at(9, 10, 23, 59), ["asap", "shift_fallback"], FLEX
        )

    def test_no_clock_meaning_is_blank_and_unparsed(self) -> None:
        result = self.check(
            "kung kelan matapos yung render", at(8, 26, 9), None, ["deadline_unparsed"]
        )
        self.assertEqual(result.source, "blank")

    def test_empty_or_missing_is_blank_without_finding(self) -> None:
        for raw in [None, "", "   ", "\xa0"]:
            self.check(raw, at(8, 26, 9), None)


class DeadlineRuleTests(unittest.TestCase):
    def test_placeholders_are_blank_without_finding(self) -> None:
        for raw in ["-", "N/A", "n/a", "none", "None"]:
            result = resolve_deadline(raw, at(9, 1, 9))
            self.assertIsNone(result.deadline)
            self.assertEqual(result.findings, [])

    def test_overnight_shift_resolves_to_next_morning(self) -> None:
        result = resolve_deadline("end of shift", at(9, 1, 22), NIGHT)
        self.assertEqual(result.deadline, at(9, 2, 5))

    def test_shift_already_ended_today_rolls_to_tomorrow(self) -> None:
        result = resolve_deadline("end of shift", at(9, 1, 20), DAY)
        self.assertEqual(result.deadline, at(9, 2, 19))

    def test_shift_hour_exactly_at_posting_time_is_kept(self) -> None:
        result = resolve_deadline("end of shift", at(9, 1, 19), DAY)
        self.assertEqual(result.deadline, at(9, 1, 19))

    def test_latest_shift_across_assignees(self) -> None:
        result = resolve_deadline("within shift", at(9, 1, 10), DAY + NIGHT)
        self.assertEqual(result.deadline, at(9, 2, 5))
        self.assertEqual(result.findings, [])

    def test_flexible_assignee_is_skipped_when_another_resolves(self) -> None:
        result = resolve_deadline("within shift", at(9, 1, 10), FLEX + DAY)
        self.assertEqual(result.deadline, at(9, 1, 19))
        self.assertEqual(result.source, "shift")
        self.assertEqual(result.findings, [])

    def test_blank_shift_values_are_defaulted_by_name(self) -> None:
        result = resolve_deadline("end of shift", at(9, 1, 10), [person("A", None), person("B", None)])
        self.assertEqual(result.deadline, at(9, 1, 23, 59))
        self.assertEqual(result.findings[0].params, {"defaulted": ["A", "B"]})

    def test_no_assignee_resolved_fallback(self) -> None:
        result = resolve_deadline("asap", at(9, 1, 10), [])
        self.assertEqual(result.deadline, at(9, 1, 23, 59))
        self.assertEqual(codes(result), ["asap", "shift_fallback"])
        self.assertEqual(result.findings[1].params, {"reason": "no assignee resolved"})

    def test_year_closest_to_posting_date(self) -> None:
        result = resolve_deadline("Dec 30", at(1, 2, 10, year=2027))
        self.assertEqual(result.deadline, at(12, 30, 23, 59, year=2026))
        self.assertEqual(codes(result), ["deadline_past"])
        result = resolve_deadline("Jan 3", at(12, 30, 10, year=2026))
        self.assertEqual(result.deadline, at(1, 3, 23, 59, year=2027))

    def test_past_stated_date_is_kept(self) -> None:
        result = resolve_deadline("August 15, Tuesday, 3PM", at(8, 17, 10))
        self.assertEqual(result.deadline, at(8, 15, 15))
        self.assertEqual(codes(result), ["deadline_past"])

    def test_explicit_past_year_is_honored_not_replaced_by_the_closest_year(self) -> None:
        result = resolve_deadline("September 16, 2020 5PM", at(9, 15, 10))
        self.assertEqual(result.deadline, at(9, 16, 17, year=2020))
        self.assertEqual(codes(result), ["deadline_past"])

    def test_explicit_future_year_is_honored(self) -> None:
        result = resolve_deadline("Dec 30, 2028", at(1, 2, 10))
        self.assertEqual(result.deadline, at(12, 30, 23, 59, year=2028))
        self.assertEqual(codes(result), [])

    def test_explicit_year_without_comma_or_with_weekday(self) -> None:
        p = at(9, 1, 10)
        self.assertEqual(resolve_deadline("Sept 5 2027", p).deadline, at(9, 5, 23, 59, year=2027))
        result = resolve_deadline("Friday, September 3, 2027, 7PM", p)
        self.assertEqual(result.deadline, at(9, 3, 19, year=2027))

    def test_explicit_year_shift_deadline_uses_that_year(self) -> None:
        result = resolve_deadline("Aug 26, 2027 end of shift", at(8, 26, 9), NIGHT)
        self.assertEqual(result.deadline, at(8, 27, 5, year=2027))

    def test_impossible_explicit_year_date_fails_closed(self) -> None:
        result = resolve_deadline("Feb 29, 2027", at(9, 1, 10))
        self.assertIsNone(result.deadline)
        self.assertEqual(codes(result), ["deadline_unparsed"])

    def test_clock_digits_after_the_day_are_not_a_year(self) -> None:
        result = resolve_deadline("September 26 1130 PM", at(9, 1, 10))
        self.assertEqual(result.deadline, at(9, 26, 23, 30))

    def test_time_formats(self) -> None:
        p = at(9, 1, 0, 30)
        for raw, expected in [
            ("5AM", at(9, 1, 5)),
            ("5:00 AM", at(9, 1, 5)),
            ("7pm", at(9, 1, 19)),
            ("530PM", at(9, 1, 17, 30)),
            ("12:00 PM", at(9, 1, 12)),
            ("11pm", at(9, 1, 23)),
        ]:
            self.assertEqual(resolve_deadline(raw, p).deadline, expected, raw)

    def test_midnight_is_start_of_stated_day(self) -> None:
        result = resolve_deadline("September 2, 12:00 AM", at(9, 1, 10))
        self.assertEqual(result.deadline, at(9, 2, 0))

    def test_unsupported_relative_words_fail_closed(self) -> None:
        for raw in ["tomorrow 3pm", "tomorrow", "next week", "13pm", "Feb 30"]:
            result = resolve_deadline(raw, at(9, 1, 10))
            self.assertIsNone(result.deadline, raw)
            self.assertEqual(codes(result), ["deadline_unparsed"], raw)

    def test_output_carries_manila_offset(self) -> None:
        result = resolve_deadline("Sep 26", datetime.fromisoformat("2026-09-22T02:00:00+00:00"))
        self.assertEqual(str(result.deadline and result.deadline.isoformat()), "2026-09-26T23:59:00+08:00")

    def test_posting_time_is_read_in_manila(self) -> None:
        # 17:30 UTC on Sep 21 is 01:30 on Sep 22 in Manila.
        result = resolve_deadline("tonight", datetime.fromisoformat("2026-09-21T17:30:00+00:00"))
        self.assertEqual(result.deadline, at(9, 22, 23, 59))


class HedgeTests(unittest.TestCase):
    def test_strip_hedges(self) -> None:
        cases = {
            "September 9, 8PM (if keri)": "September 9, 8PM",
            "Tonight if keri or tom": "Tonight",
            "If keri rin po today": "today",
            "end of shift if kaya, if not tomorrow na po": "end of shift",
            "kung kelan matapos yung render": "matapos yung render",
        }
        for raw, expected in cases.items():
            self.assertEqual(strip_hedges(raw), expected, raw)


class StudioTimeTests(unittest.TestCase):
    def test_one_manila_constant_is_shared(self) -> None:
        from t6.domain import dateformats
        from t6.domain import deadlines as deadline_module

        self.assertIs(dateformats.MANILA, MANILA)
        self.assertIs(getattr(deadline_module, "MANILA"), MANILA)

    def test_shift_reports_each_open_where_the_previous_went_out(self) -> None:
        from t6.domain.studio_time import SHIFT_REPORT_SLOTS

        slots = SHIFT_REPORT_SLOTS
        for index, slot in enumerate(slots):
            self.assertEqual(slot.opens_at, slots[index - 1].at)


if __name__ == "__main__":
    unittest.main()
