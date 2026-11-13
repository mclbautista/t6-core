"""Coordinators, the standing cc and operator IDs. Names here are made up."""

import unittest

from t6.domain import Contact, Project
from t6.domain.studio_people import (
    STANDING_CC_FINDINGS,
    coordinators,
    operator_ids,
    standing_cc,
    tags_standing_cc,
    with_standing_cc,
)

ALL_FINDINGS = [
    "unsure", "multiple_jo", "unknown_department", "exact_repost", "number_reuse", "forward_origin",
    "title_blank", "task_blank", "deadline_past", "deadline_unparsed", "asap", "shift_fallback",
    "header_date_mismatch", "deliverable_hint", "matcher_skipped", "readback_mismatch", "duplicate_key",
    "unresolved_person", "ambiguous_person", "unresolved_project",
]


class CoordinatorTests(unittest.TestCase):
    def test_a_projects_coordinators_come_with_their_telegram_ids(self) -> None:
        ann = Contact(page_id="c1", name="Ann Example", telegram_user_id=111, telegram_handle="ann_ex")
        bo = Contact(page_id="c2", name="Bo Example", telegram_user_id=222)
        other = Contact(page_id="c3", name="Cy Example")
        project = Project(page_id="p1", name="Show", post_coordinator_ids=("c2", "c1", "gone"))
        found = coordinators(project, {c.page_id: c for c in (ann, bo, other)})
        self.assertEqual([(c.name, c.telegram_user_id) for c in found], [("Bo Example", 222), ("Ann Example", 111)])

    def test_no_coordinator_is_an_empty_tuple(self) -> None:
        self.assertEqual(coordinators(Project(page_id="p1", name="Show"), {}), ())


class StandingCcTests(unittest.TestCase):
    def test_setting_is_a_contact_page_id_or_none(self) -> None:
        self.assertEqual(standing_cc({"T6_STANDING_CC": " page-1 "}), "page-1")
        self.assertIsNone(standing_cc({}))
        self.assertIsNone(standing_cc({"T6_STANDING_CC": "  "}))

    def test_added_to_every_cc_last_and_once(self) -> None:
        env = {"T6_STANDING_CC": "sc"}
        self.assertEqual(with_standing_cc(["a", "b"], env), ("a", "b", "sc"))
        self.assertEqual(with_standing_cc([], env), ("sc",))
        self.assertEqual(with_standing_cc(["sc", "a"], env), ("sc", "a"))
        self.assertEqual(with_standing_cc(["a", "a"], {}), ("a",))

    def test_only_findings_needing_their_action_tag_them(self) -> None:
        tagged = {"multiple_jo", "unresolved_person", "ambiguous_person", "unresolved_project", "unknown_department"}
        self.assertEqual(STANDING_CC_FINDINGS, tagged)
        for code in ALL_FINDINGS:
            with self.subTest(code=code):
                self.assertEqual(tags_standing_cc([code]), code in tagged)
        self.assertTrue(tags_standing_cc(["asap", "multiple_jo"]))
        self.assertFalse(tags_standing_cc([]))


class OperatorIdTests(unittest.TestCase):
    def test_ids_split_on_spaces_and_commas(self) -> None:
        self.assertEqual(operator_ids({"T6_OPERATOR_IDS": "1, 2 3,4"}), (1, 2, 3, 4))
        self.assertEqual(operator_ids({}), ())

    def test_a_bad_id_raises(self) -> None:
        with self.assertRaises(ValueError):
            operator_ids({"T6_OPERATOR_IDS": "1 x"})


if __name__ == "__main__":
    unittest.main()
