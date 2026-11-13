from datetime import date, datetime, timedelta, timezone

import unittest

from t6.domain import dateformats as f

# 2026-11-13 is a Friday. 15:59 UTC is 23:59 in Manila.
UTC = timezone.utc


class DateFormats(unittest.TestCase):

    def test_deadline_in_manila_time(self) -> None:
        self.assertEqual(f.deadline(datetime(2026, 11, 13, 15, 59, tzinfo=UTC)), "Nov 13 / Fri (23:59)")


    def test_deadline_converts_other_zones_and_crosses_midnight(self) -> None:
        self.assertEqual(f.deadline(datetime(2026, 11, 13, 20, 0, tzinfo=UTC)), "Nov 14 / Sat (04:00)")
        self.assertEqual(f.deadline(datetime(2026, 11, 3, 0, 0, tzinfo=timezone(timedelta(hours=-5)))), "Nov 3 / Tue (13:00)")


    def test_naive_datetime_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            f.deadline(datetime(2026, 11, 13, 23, 59))


    def test_report_header(self) -> None:
        self.assertEqual(f.report_header(datetime(2026, 11, 13, 1, 0, tzinfo=UTC)), "Fri, Nov 13, 2026")


    def test_long_date_from_datetime_and_epoch(self) -> None:
        moment = datetime(2026, 11, 12, 11, 5, tzinfo=UTC)
        self.assertEqual(f.long_date(moment), "12 Nov 2026, 19:05 (Manila)")
        self.assertEqual(f.long_date(moment.timestamp()), "12 Nov 2026, 19:05 (Manila)")


    def test_briefing_date(self) -> None:
        self.assertEqual(f.briefing_date(date(2026, 11, 14)), "Sat Nov 14")


    def test_flag_date(self) -> None:
        self.assertEqual(f.flag_date(date(2026, 12, 1)), "Dec 1, 2026")


    def test_concern_age(self) -> None:
        since = date(2026, 10, 26)
        self.assertEqual(f.concern_age(since, date(2026, 11, 9)), "since Oct 26 (2 wks)")
        self.assertEqual(f.concern_age(since, date(2026, 11, 2)), "since Oct 26 (1 wk)")
        self.assertEqual(f.concern_age(since, date(2026, 10, 27)), "since Oct 26 (1 day)")
        self.assertEqual(f.concern_age(since, date(2026, 10, 26)), "since Oct 26 (0 days)")
        with self.assertRaises(ValueError):
            f.concern_age(date(2026, 11, 1), date(2026, 10, 1))


    def test_minutes_title(self) -> None:
        self.assertEqual(f.minutes_title(date(2026, 11, 16)), "PSR MOM 2026-11-16")


    def test_formatter_date(self) -> None:
        self.assertEqual(f.formatter_date(date(2026, 7, 23)), "July 23 / Thursday")


if __name__ == "__main__":
    unittest.main()
