"""The studio's one contact matching policy, as table tests.

Order: Telegram ID, handle, alias or name, then fuzzy (rapidfuzz WRatio, accept at 90 with a lead of 8).
Suggestions are a separate result and never decide. The cases marked "apps differ" are ones where a
stdlib difflib score at 0.92 gives another answer than WRatio at 90.
"""

import unittest

from t6.domain import Contact
from t6.lookup import Candidate, Index, Lookup, Policy


def contact(page_id: str, name: str, **kw: object) -> Contact:
    return Contact(page_id=page_id, name=name, **kw)  # type: ignore[arg-type]


def picked(lookup: Lookup, mention: str, **kw: object) -> str | None:
    found = lookup.contact(mention, **kw)  # type: ignore[arg-type]
    return found.entity.page_id if found.entity else None


class OrderTests(unittest.TestCase):
    def test_telegram_id_beats_handle_beats_alias_and_name(self) -> None:
        by_id = contact("id", "Someone Else", telegram_user_id=4242)
        by_handle = contact("handle", "Another One", telegram_handle="4242")
        by_name = contact("name", "4242")
        by_alias = contact("alias", "Third Person", aliases=("pedro",))
        by_handle_pedro = contact("handle2", "Fourth Person", telegram_handle="@Pedro")
        lookup = Lookup(Index(contacts=(by_id, by_handle, by_name, by_alias, by_handle_pedro)))
        self.assertEqual(picked(lookup, "4242"), "id")
        self.assertEqual(picked(lookup, "pedro"), "handle2")
        self.assertEqual(lookup.contact("pedro").method, "normalized")

    def test_alias_and_name_share_a_stage_so_two_matches_are_a_tie(self) -> None:
        lookup = Lookup(Index(contacts=(contact("a", "Mars Cruz"), contact("b", "Other Person", aliases=("Mars Cruz",)))))
        found = lookup.contact("Mars Cruz")
        self.assertEqual((found.entity, found.needs_review), (None, True))

    def test_project_team_is_searched_first(self) -> None:
        lookup = Lookup(Index(contacts=(contact("a", "Maria Santos"), contact("b", "Maria Santos"))))
        self.assertEqual(picked(lookup, "Maria Santos", team=("b",)), "b")


class HonorificTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lookup = Lookup(Index(contacts=(contact("jun", "Jun Dela Cruz"), contact("ate", "Ate Rose"), contact("sir", "Sir"))))

    def test_removed_at_the_start_whatever_the_count(self) -> None:
        for mention in ("Kuya Jun Dela Cruz", "ate kuya Jun Dela Cruz", "Atty. Jun Dela Cruz", "Engr Jun Dela Cruz"):
            with self.subTest(mention=mention):
                self.assertEqual(picked(self.lookup, mention), "jun")

    def test_not_removed_from_the_middle_of_a_name(self) -> None:
        # "Jun Sir Cruz" keeps "sir": it must not become "Jun Cruz".
        lookup = Lookup(Index(contacts=(contact("a", "Jun Sir Cruz"),)))
        self.assertEqual(picked(lookup, "Jun Sir Cruz"), "a")
        self.assertEqual(lookup.contact("Jun Cruz").entity, None)

    def test_at_least_one_word_is_kept(self) -> None:
        self.assertEqual(picked(self.lookup, "Sir"), "sir")

    def test_a_stored_honorific_is_removed_from_the_name_too(self) -> None:
        self.assertEqual(picked(self.lookup, "Rose"), "ate")


class ThresholdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.anne = contact("anne", "Anne Reyes")
        self.pedro = contact("pedro", "Pedro Santos")
        self.lookup = Lookup(Index(contacts=(self.anne, self.pedro)))

    def test_wratio_90_is_accepted_where_difflib_92_would_not_be(self) -> None:
        # apps differ: "anna reyes" vs "anne reyes" is 90.0 WRatio and 0.90 difflib.
        found = self.lookup.contact("Anna Reyes")
        self.assertEqual((found.entity, found.method, found.needs_review), (self.anne, "fuzzy", False))
        self.assertAlmostEqual(found.score, 0.90)

    def test_a_typo_in_a_longer_name_is_accepted(self) -> None:
        # apps differ: 91.67 WRatio, 0.917 difflib.
        found = Lookup(Index(contacts=(contact("m", "Maria Santos"), self.pedro))).contact("Maria Santoz")
        self.assertEqual((found.entity.page_id if found.entity else None, found.method), ("m", "fuzzy"))

    def test_below_90_is_review_not_a_pick(self) -> None:
        found = self.lookup.contact("Anna Reys")
        self.assertIsNone(found.entity)
        self.assertEqual(found.method, "review")
        self.assertTrue(found.needs_review)

    def test_a_lead_under_8_over_the_runner_up_is_review(self) -> None:
        lookup = Lookup(Index(contacts=(contact("a", "Maria Santos"), contact("b", "Maria Santoz"))))
        found = lookup.contact("Maria Santox")
        self.assertIsNone(found.entity)
        self.assertTrue(found.needs_review)
        self.assertEqual(sorted(c.entity.page_id for c in found.candidates), ["a", "b"])

    def test_a_lead_of_exactly_8_is_accepted(self) -> None:
        # 0.98 - 0.90 is 0.0799... in floating point; the lead is still 8.
        scores = {"ana lim": 0.98, "bea lim": 0.90}
        policy = Policy(fuzzy_accept=0.90, margin=0.08, review_floor=0.60, scorer=lambda w, f: scores.get(f, 0.0))
        lookup = Lookup(Index(contacts=(contact("a", "Ana Lim"), contact("b", "Bea Lim"))), policies={"contact": policy})
        self.assertEqual(picked(lookup, "xx lim"), "a")

    def test_candidates_are_at_most_3(self) -> None:
        contacts = tuple(contact(f"c{n}", f"Maria Santos{'abc'[n % 3]}{n}") for n in range(6))
        found = Lookup(Index(contacts=contacts)).contact("Maria Santos")
        self.assertLessEqual(len(found.candidates), 3)


class SuggestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contacts = (
            contact("a", "Pedro Reyes"),
            contact("b", "Pedra Reyes"),
            contact("c", "Pedro Reyez"),
            contact("d", "Pedro Reyess"),
            contact("e", "Zed Quill"),
        )
        self.lookup = Lookup(Index(contacts=self.contacts))

    def test_up_to_3_best_first_at_wratio_60_or_more(self) -> None:
        found = self.lookup.suggest_contacts("Pedro Reyes")
        self.assertEqual(len(found), 3)
        self.assertEqual(found[0].entity.page_id, "a")
        self.assertTrue(all(isinstance(c, Candidate) and c.score >= 0.60 for c in found))
        self.assertEqual([c.score for c in found], sorted((c.score for c in found), reverse=True))

    def test_nothing_below_60_is_suggested(self) -> None:
        self.assertEqual(self.lookup.suggest_contacts("Qqqqq"), ())

    def test_suggestions_are_not_a_resolution(self) -> None:
        found = self.lookup.suggest_contacts("Pedro")
        self.assertTrue(found)
        self.assertTrue(all(isinstance(c, Candidate) for c in found))

    def test_team_members_come_first_among_equal_scores(self) -> None:
        twins = (contact("x", "Lena Cruz"), contact("y", "Lena Cruz"))
        found = Lookup(Index(contacts=twins)).suggest_contacts("Lena Cruz", team=("y",))
        self.assertEqual([c.entity.page_id for c in found], ["y", "x"])


if __name__ == "__main__":
    unittest.main()
