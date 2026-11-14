"""Contract tests for reversible masking through the public t6.privacy interface.

The regression cases from mclbautista/t6-notion-reports#67 are carried over here:
placeholder variants (case, spacing, missing brackets), one placeholder shared by
two casings of a name, and placeholders the session never issued.
"""

import dataclasses
import unittest
from typing import Any

from t6.privacy import Leak, Masked, Person, Project, Registry, Session, Unresolved, pseudonymize


def registry() -> Registry:
    return Registry(
        people=[Person("Marco Rossi", aliases=["Marco", "Rossi"], handles=["@marcor"])],
        projects=[Project("Other Show", code="OS1"), Project("Own Show", code="OWN")],
        ids=["123456789"],
    )


@dataclasses.dataclass(frozen=True)
class Change:
    text: str
    quote: str
    line_id: int


@dataclasses.dataclass(frozen=True)
class Reply:
    changes: tuple[Change, ...]
    concerns: list[str]
    notes: dict[str, str]


class PseudonymizeTests(unittest.TestCase):
    def test_returns_masked_text_and_session(self) -> None:
        masked, session = pseudonymize("Marco confirmed Other Show 123456789", registry())
        self.assertIsInstance(masked, Masked)
        self.assertIsInstance(session, Session)
        self.assertEqual(masked.text, "[person 1] confirmed [project 1] [id 1]")

    def test_masked_is_not_a_plain_string(self) -> None:
        masked, _ = pseudonymize("Marco", registry())
        self.assertNotIsInstance(masked, str)

    def test_every_registered_form_is_masked(self) -> None:
        masked, session = pseudonymize("Marco Rossi, Marco, Rossi, @marcor, OS1, Other Show and 123456789", registry())
        self.assertEqual(session.leaks(masked.text), [])
        for raw in ("Marco Rossi", "Marco", "Rossi", "@marcor", "OS1", "Other Show", "123456789"):
            self.assertNotIn(raw.casefold(), masked.text.casefold())

    def test_matching_is_by_whole_word(self) -> None:
        masked, _ = pseudonymize("Marcos and 1234567890 and BOS1", registry())
        self.assertEqual(masked.text, "Marcos and 1234567890 and BOS1")

    def test_matching_ignores_case(self) -> None:
        masked, _ = pseudonymize("marco and OTHER SHOW", registry())
        self.assertEqual(masked.text, "[person 1] and [project 1]")

    def test_names_in_allow_stay_unmasked(self) -> None:
        masked, _ = pseudonymize("Own Show (OWN) with Marco", registry(), allow=["Own Show", "own"])
        self.assertEqual(masked.text, "Own Show (OWN) with [person 1]")

    def test_allowed_longer_name_is_not_masked_by_a_shorter_one(self) -> None:
        reg = Registry(people=[Person("Show")], projects=[Project("Own Show")])
        masked, _ = pseudonymize("Own Show and Show", reg, allow=["Own Show"])
        self.assertEqual(masked.text, "Own Show and [person 1]")

    def test_two_casings_share_one_placeholder(self) -> None:
        masked, session = pseudonymize("Marco said MARCO", registry())
        self.assertEqual(masked.text, "[person 1] said [person 1]")
        self.assertEqual(session.restore("[person 1] and [PERSON 1]"), "Marco and Marco")

    def test_distinct_names_get_numbered_placeholders(self) -> None:
        masked, _ = pseudonymize("Marco and Rossi on Other Show and OS1", registry())
        self.assertEqual(masked.text, "[person 1] and [person 2] on [project 1] and [project 2]")

    def test_session_masks_more_fields_with_the_same_placeholders(self) -> None:
        masked, session = pseudonymize("Marco on Other Show", registry())
        second = session.mask("Other Show and Own Show")
        self.assertIsInstance(second, Masked)
        self.assertEqual(masked.text, "[person 1] on [project 1]")
        self.assertEqual(second.text, "[project 1] and [project 2]")
        self.assertEqual(session.restore("[project 2] after [person 1]"), "Own Show after Marco")


class RestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        _, self.session = pseudonymize("Waiting for Marco", registry())

    def test_placeholder_variants_restore_the_original(self) -> None:
        variants = [
            "[person 1]", "[Person 1]", "[PERSON 1]", "[person  1]",
            "person 1", "[person 1 ]", "[ person1 ]", "[ PERSON 1 ]",
        ]
        for token in variants:
            with self.subTest(token=token):
                self.assertEqual(self.session.restore(f"Waiting for {token}"), "Waiting for Marco")

    def test_placeholder_the_session_did_not_issue_raises_unresolved(self) -> None:
        with self.assertRaises(Unresolved) as caught:
            self.session.restore("Waiting for [person 1] and [person 8]")
        self.assertEqual(caught.exception.tokens, (("", "[person 8]"),))

    def test_unissued_placeholder_is_not_restored_to_another_name(self) -> None:
        _, session = pseudonymize("Marco and 123456789", registry())
        with self.assertRaises(Unresolved) as caught:
            session.restore("[id 1] and [id 2]")
        self.assertEqual(caught.exception.tokens, (("", "[id 2]"),))

    def test_text_without_placeholders_is_unchanged(self) -> None:
        self.assertEqual(self.session.restore("QC pending"), "QC pending")

    def test_walks_an_output_model_and_keeps_its_type(self) -> None:
        reply = Reply(
            changes=(Change("Pending [PERSON 1]", "Pending [person  1]", 1),),
            concerns=["person 1 is away"],
            notes={"who": "[ person1 ]"},
        )
        restored = self.session.restore(reply)
        self.assertEqual(
            restored,
            Reply(
                changes=(Change("Pending Marco", "Pending Marco", 1),),
                concerns=["Marco is away"],
                notes={"who": "Marco"},
            ),
        )

    def test_unresolved_reports_every_path_and_restores_nothing(self) -> None:
        reply = Reply(
            changes=(Change("Pending [person 8]", "QC pending", 1),),
            concerns=["[person 1]", "[project 3] late"],
            notes={},
        )
        with self.assertRaises(Unresolved) as caught:
            self.session.restore(reply)
        self.assertEqual(
            caught.exception.tokens,
            (("changes[0].text", "[person 8]"), ("concerns[1]", "[project 3]")),
        )

    def test_walks_pydantic_style_models(self) -> None:
        class Model:
            model_fields: dict[str, Any] = {"who": None, "count": None}

            def __init__(self, who: str, count: int) -> None:
                self.who, self.count = who, count

            def model_copy(self, *, update: dict[str, Any]) -> "Model":
                return Model(update.get("who", self.who), update.get("count", self.count))

        restored = self.session.restore(Model("[PERSON 1 ]", 3))
        self.assertEqual((restored.who, restored.count), ("Marco", 3))

    def test_restores_ids_it_issued(self) -> None:
        _, session = pseudonymize("ping 123456789", registry())
        self.assertEqual(session.restore("ping [ID 1]"), "ping 123456789")


class LeakTests(unittest.TestCase):
    def test_finds_raw_registry_name_in_output_model(self) -> None:
        _, session = pseudonymize("Marco on Other Show", registry())
        reply = Reply(
            changes=(Change("Pending [person 1]", "Rossi said so", 1),),
            concerns=["other show slipped"],
            notes={},
        )
        self.assertEqual(
            session.leaks(reply),
            [Leak("changes[0].quote", "Rossi"), Leak("concerns[0]", "other show")],
        )

    def test_clean_output_has_no_leaks(self) -> None:
        _, session = pseudonymize("Marco", registry())
        self.assertEqual(session.leaks(["[person 1] is away", "QC pending"]), [])

    def test_allowed_names_are_not_leaks(self) -> None:
        _, session = pseudonymize("Own Show", registry(), allow=["Own Show"])
        self.assertEqual(session.leaks("Own Show is on track"), [])


if __name__ == "__main__":
    unittest.main()
