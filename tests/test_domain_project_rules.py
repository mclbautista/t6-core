"""Studio Project rules, ported from t6-notion-reports' flag, gate and Health tests."""

import datetime as dt
import unittest

from t6.domain import Project
from t6.domain.project_rules import (
    ROLES,
    DateFlag,
    StatusGroup,
    code_labels,
    date_flags,
    health_missing,
    health_rank,
    is_snapshotted,
    missing_roles,
    status_gate,
)

TODAY = dt.date(2026, 11, 14)


def project(status: str, **fields: object) -> Project:
    return Project(page_id="p1", name="One", status=status, **fields)  # type: ignore[arg-type]


class StatusGateTests(unittest.TestCase):
    def test_gate_groups_by_status_ignoring_emoji_case_and_spaces(self) -> None:
        table = {
            "🚨 In-Progress": StatusGroup.IN_PROGRESS,
            "🏎. In-Progress TVC": StatusGroup.IN_PROGRESS,
            "Airing": StatusGroup.IN_PROGRESS,
            "in-progress": StatusGroup.IN_PROGRESS,
            "🏋️ Standby": StatusGroup.ON_HOLD,
            "📦 Archiving": StatusGroup.ON_HOLD,
            "🌱 Leads": StatusGroup.INCOMING,
            "Lead": StatusGroup.INCOMING,
            "  Negotiations ": StatusGroup.INCOMING,
            "Proposal  Made": StatusGroup.INCOMING,
            "Archived": None,
            "Lost": None,
            "": None,
            "Something else": None,
        }
        for status, group in table.items():
            with self.subTest(status=status):
                self.assertEqual(status_gate(status), group)

    def test_archived_and_lost_are_never_snapshotted(self) -> None:
        self.assertFalse(is_snapshotted("🗄 Archived"))
        self.assertFalse(is_snapshotted("Lost"))
        self.assertTrue(is_snapshotted("📦 Archiving"))
        self.assertTrue(is_snapshotted(None))


class DateFlagTests(unittest.TestCase):
    def test_in_progress_and_tvc_flag_blank_playdate_and_submission_deadline(self) -> None:
        for status in ("🚨 In-Progress", "🏎. In-Progress TVC", "Airing"):
            with self.subTest(status=status):
                self.assertEqual(
                    date_flags(project(status), TODAY),
                    (DateFlag("missing", "playdate"), DateFlag("missing", "submission_deadline")),
                )

    def test_overdue_comes_first_and_counts_days(self) -> None:
        flags = date_flags(project("In-Progress", submission_deadline=dt.date(2026, 11, 10)), TODAY)
        self.assertEqual(
            flags, (DateFlag("overdue", "submission_deadline", 4), DateFlag("missing", "playdate"))
        )

    def test_today_is_not_overdue(self) -> None:
        self.assertEqual(
            date_flags(project("In-Progress", playdate=TODAY, submission_deadline=TODAY), TODAY), ()
        )

    def test_a_datetime_is_judged_by_its_manila_day(self) -> None:
        # 17:00 UTC on the 13th is 01:00 on the 14th in Manila: not overdue.
        late = dt.datetime(2026, 11, 13, 17, 0, tzinfo=dt.timezone.utc)
        flags = date_flags(project("In-Progress", playdate=late, submission_deadline=TODAY), TODAY)
        self.assertEqual(flags, ())

    def test_airing_is_never_overdue(self) -> None:
        past = dt.date(2026, 1, 1)
        self.assertEqual(date_flags(project("Airing", playdate=past, submission_deadline=past), TODAY), ())

    def test_standby_never_flags_dates(self) -> None:
        self.assertEqual(date_flags(project("🏋️ Standby", playdate=dt.date(2020, 1, 1)), TODAY), ())

    def test_archiving_flags_only_the_archival_deadline(self) -> None:
        self.assertEqual(date_flags(project("📦 Archiving"), TODAY), (DateFlag("missing", "archival_deadline"),))
        self.assertEqual(
            date_flags(project("📦 Archiving", archival_deadline=dt.date(2026, 11, 13)), TODAY),
            (DateFlag("overdue", "archival_deadline", 1),),
        )

    def test_incoming_flags_an_overdue_or_near_playdate_never_a_blank_one(self) -> None:
        for status in ("Leads", "Lead", "Negotiations", "Proposal made"):
            with self.subTest(status=status):
                self.assertEqual(date_flags(project(status), TODAY), ())
                self.assertEqual(
                    date_flags(project(status, playdate=dt.date(2026, 11, 10)), TODAY),
                    (DateFlag("overdue", "playdate", 4),),
                )
                self.assertEqual(
                    date_flags(project(status, playdate=dt.date(2026, 12, 13)), TODAY),
                    (DateFlag("upcoming", "playdate", 29),),
                )
                self.assertEqual(date_flags(project(status, playdate=dt.date(2026, 12, 14)), TODAY), ())

    def test_the_lead_warning_window_is_configurable(self) -> None:
        soon = project("Leads", playdate=dt.date(2026, 11, 20))
        self.assertEqual(date_flags(soon, TODAY, lead_warning_days=5), ())
        self.assertEqual(date_flags(soon, TODAY, lead_warning_days=7), (DateFlag("upcoming", "playdate", 6),))


class RoleAndHealthTests(unittest.TestCase):
    def test_missing_roles_lists_every_empty_role_for_dated_statuses_only(self) -> None:
        staffed = {role: ("c1",) for role in ROLES}
        self.assertEqual(missing_roles(project("In-Progress", **staffed)), ())
        self.assertEqual(missing_roles(project("In-Progress", **{**staffed, "director_ids": ()})), ("director_ids",))
        self.assertEqual(missing_roles(project("Airing")), ROLES)
        self.assertEqual(missing_roles(project("Standby")), ())
        self.assertEqual(missing_roles(project("Leads")), ())

    def test_roles_cover_the_assistant_editor_coordinator_director_and_cinematographer(self) -> None:
        for role in ("assistant_editor_ids", "post_coordinator_ids", "director_ids", "cinematographer_ids"):
            self.assertIn(role, ROLES)

    def test_blank_health_is_flagged(self) -> None:
        self.assertTrue(health_missing(project("Standby")))
        self.assertTrue(health_missing(project("Standby", health="  ")))
        self.assertFalse(health_missing(project("Standby", health="ON TRACK")))

    def test_health_order(self) -> None:
        values = ["", "Weird", "INCOMING", "ON TRACK", "AT RISK", "CRITICAL", None]
        self.assertEqual(
            sorted(values, key=health_rank), ["CRITICAL", "AT RISK", "ON TRACK", "INCOMING", "Weird", "", None]
        )


class CodeLabelTests(unittest.TestCase):
    def test_code_in_capitals_or_code_and_name_when_shared(self) -> None:
        a = Project(page_id="a", name="Alpha", project_code="abc")
        b = Project(page_id="b", name="Beta", project_code="XYZ")
        c = Project(page_id="c", name="Gamma", project_code="Abc")
        d = Project(page_id="d", name="Delta")
        self.assertEqual(
            code_labels([a, b, c, d]),
            {"a": "ABC (Alpha)", "b": "XYZ", "c": "ABC (Gamma)", "d": "Delta"},
        )
        self.assertEqual(code_labels([a, b]), {"a": "ABC", "b": "XYZ"})


if __name__ == "__main__":
    unittest.main()
