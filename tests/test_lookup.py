"""Exercise t6.lookup through its public interface only.

Seams under test: `Lookup` methods in, `Resolution` out. Entities are plain
`t6.domain` values; the LLM strategy is a plain callable (a fake here, and a
scripted Jev through `t6.llm` in one test); Notion is the in-memory fake.
"""

import dataclasses
import json
import unittest
from collections.abc import Mapping, Sequence

from t6.domain import Contact, Deliverable, JobOrder, Machine, NotionEntity, Project, Studio
from t6.llm import CandidateDecisions, CandidateKeys, Choice, DataPolicy, DecisionsRequest, Ok, Task, candidate_key, run
from t6.llm.testing import FakeProvider
from t6.lookup import AliasWrite, Index, Lookup, Policy, Resolution
from t6.notion.models import project_from_page
from t6.notion.testing import FakeNotion, project_properties
from t6.privacy import Withheld, withhold


def contact(page_id: str, name: str, **kw: object) -> Contact:
    return Contact(page_id=page_id, name=name, **kw)  # type: ignore[arg-type]


def ids(resolution: Resolution[NotionEntity]) -> list[str]:
    return [c.entity.page_id for c in resolution.candidates]


class JobOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.jo = JobOrder(page_id="jo1", name="Conform ep 101", telegram_key="JO-1042")
        self.lookup = Lookup(Index(job_orders=(self.jo, JobOrder(page_id="jo2", name="Other", telegram_key="JO-7"))))

    def test_telegram_key_returns_the_job_order(self) -> None:
        r = self.lookup.job_order("JO-1042")
        self.assertEqual(r.entity, self.jo)
        self.assertEqual((r.method, r.score, r.needs_review), ("exact_id", 1.0, False))

    def test_a_retyped_key_is_a_normalized_match(self) -> None:
        r = self.lookup.job_order(" jo1042 ")
        self.assertEqual((r.entity, r.method), (self.jo, "normalized"))

    def test_an_unknown_key_returns_none_and_is_not_a_review(self) -> None:
        r = self.lookup.job_order("JO-9999")
        self.assertEqual((r.entity, r.method, r.needs_review, r.candidates), (None, "none", False, ()))

    def test_a_duplicate_key_needs_review(self) -> None:
        twin = JobOrder(page_id="jo3", name="Twin", telegram_key="JO-1042")
        r = Lookup(Index(job_orders=(self.jo, twin))).job_order("JO-1042")
        self.assertEqual((r.entity, r.needs_review), (None, True))
        self.assertEqual(ids(r), ["jo1", "jo3"])


class ProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.a = Project(page_id="p1", name="Blue Harbor", project_code="BH-101")
        self.b = Project(page_id="p2", name="Red Canyon", project_code="RC-202")
        self.lookup = Lookup(Index(projects=(self.a, self.b)))

    def test_code_returns_the_project(self) -> None:
        r = self.lookup.project("RC-202")
        self.assertEqual((r.entity, r.method), (self.b, "exact_id"))

    def test_code_ignores_case_and_separators(self) -> None:
        self.assertEqual(self.lookup.project("bh 101").entity, self.a)

    def test_duplicate_code_needs_review_with_both_candidates(self) -> None:
        dupe = Project(page_id="p3", name="Blue Harbour Redux", project_code="BH-101")
        r = Lookup(Index(projects=(self.a, self.b, dupe))).project("BH-101")
        self.assertEqual((r.entity, r.needs_review), (None, True))
        self.assertEqual(ids(r), ["p1", "p3"])

    def test_blank_code_needs_review_and_ranks_by_name(self) -> None:
        r = self.lookup.project("", name="Blue Harbor")
        self.assertEqual((r.entity, r.needs_review), (None, True))
        self.assertEqual(ids(r)[0], "p1")

    def test_blank_code_and_no_name_lists_projects_missing_a_code(self) -> None:
        uncoded = Project(page_id="p9", name="Draft Show")
        r = Lookup(Index(projects=(self.a, uncoded))).project(None)
        self.assertEqual((r.entity, r.needs_review, ids(r)), (None, True, ["p9"]))

    def test_projects_read_from_the_fake_notion_resolve_by_code(self) -> None:
        fake = FakeNotion()
        fake.add_data_source("ds-projects")
        fake.add_page("ds-projects", project_properties(name="Green Valley", project_code="GV-300"), page_id="pg1")
        with fake.client() as client:
            rows = client.query_data_source("ds-projects")
        r = Lookup(Index(projects=tuple(project_from_page(row) for row in rows))).project("GV-300")
        self.assertEqual((r.entity.page_id if r.entity else None, r.method), ("pg1", "exact_id"))


class DeliverableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.master = Deliverable(page_id="d1", name="Final Master ProRes", project_id="p1")
        self.trailer = Deliverable(page_id="d2", name="Trailer", project_id="p1")
        self.elsewhere = Deliverable(page_id="d3", name="Final Master ProRes", project_id="p2")
        self.lookup = Lookup(Index(deliverables=(self.master, self.trailer, self.elsewhere)))

    def test_resolves_within_the_given_project_only(self) -> None:
        r = self.lookup.deliverable("final master prores", project_id="p1")
        self.assertEqual(r.entity, self.master)
        self.assertIsInstance(r.entity, Deliverable)

    def test_candidates_never_leave_the_project(self) -> None:
        r = self.lookup.deliverable("Final Master", project_id="p1")
        self.assertTrue(r.candidates)
        self.assertTrue(all(c.entity.project_id == "p1" for c in r.candidates))
        self.assertNotIn("d3", ids(r))

    def test_a_weak_match_is_never_picked(self) -> None:
        r = self.lookup.deliverable("closed captions", project_id="p1")
        self.assertIsNone(r.entity)
        self.assertNotEqual(r.method, "fuzzy")

    def test_a_close_but_ambiguous_match_needs_review(self) -> None:
        a = Deliverable(page_id="e1", name="Ep 101 Final Master", project_id="p1")
        b = Deliverable(page_id="e2", name="Ep 102 Final Master", project_id="p1")
        r = Lookup(Index(deliverables=(a, b))).deliverable("Ep 10 Final Master", project_id="p1")
        self.assertEqual((r.entity, r.needs_review), (None, True))
        self.assertEqual(sorted(ids(r)), ["e1", "e2"])

    def test_a_single_clear_typo_is_a_fuzzy_match(self) -> None:
        r = self.lookup.deliverable("Triler", project_id="p1")
        self.assertEqual((r.entity, r.method, r.needs_review), (self.trailer, "fuzzy", False))
        self.assertGreaterEqual(r.score, 0.8)


class ContactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.maria = contact("c1", "Maria Santos", telegram_handle="mariasantos", telegram_user_id=111, aliases=("Mars",))
        self.pedro = contact("c2", "Pedro Reyes", telegram_handle="@pedror")
        self.lookup = Lookup(Index(contacts=(self.maria, self.pedro)))

    def test_honorifics_are_stripped(self) -> None:
        for mention in ("Ate Maria Santos", "kuya pedro reyes", "Sir Pedro Reyes"):
            with self.subTest(mention=mention):
                r = self.lookup.contact(mention)
                self.assertEqual(r.entity.name if r.entity else None, mention.split(" ", 1)[1].title())

    def test_handle_with_or_without_at_sign(self) -> None:
        self.assertEqual(self.lookup.contact("@MariaSantos").entity, self.maria)
        self.assertEqual(self.lookup.contact("pedror").entity, self.pedro)

    def test_telegram_user_id_is_an_exact_id(self) -> None:
        r = self.lookup.contact("111")
        self.assertEqual((r.entity, r.method), (self.maria, "exact_id"))

    def test_alias_matches(self) -> None:
        self.assertEqual(self.lookup.contact("Mars").entity, self.maria)

    def test_team_is_searched_before_everyone(self) -> None:
        other = contact("c3", "Maria Santos")
        lookup = Lookup(Index(contacts=(self.maria, other)))
        self.assertEqual(lookup.contact("Maria Santos").needs_review, True)
        self.assertEqual(lookup.contact("Maria Santos", team=("c3",)).entity, other)

    def test_falls_back_to_all_contacts_when_the_team_has_no_match(self) -> None:
        r = self.lookup.contact("Pedro Reyes", team=("c1",))
        self.assertEqual(r.entity, self.pedro)

    def test_a_surname_shared_by_two_contacts_needs_review_not_a_pick(self) -> None:
        other = contact("c3", "Ana Santos")
        r = Lookup(Index(contacts=(self.maria, other, self.pedro))).contact("Santos")
        self.assertIsNone(r.entity)
        self.assertEqual((r.method, r.needs_review), ("review", True))
        self.assertCountEqual(ids(r), ["c1", "c3"])


class MachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mac = Machine(page_id="m1", name="Bay 2", hostname="bay2-mac", serial_number="C02XYZ123")
        self.lookup = Lookup(Index(machines=(self.mac, Machine(page_id="m2", name="Bay 3", hostname="bay3-mac"))))

    def test_hostname_returns_the_machine(self) -> None:
        self.assertEqual(self.lookup.machine("bay2-mac").entity, self.mac)

    def test_hostname_with_a_local_suffix(self) -> None:
        r = self.lookup.machine("Bay2-Mac.local")
        self.assertEqual((r.entity, r.method), (self.mac, "normalized"))

    def test_serial_number_returns_the_machine(self) -> None:
        r = self.lookup.machine("C02XYZ123")
        self.assertEqual((r.entity, r.method), (self.mac, "exact_id"))

    def test_unknown_machine_is_none(self) -> None:
        self.assertEqual(self.lookup.machine("nope").method, "none")


class StudioTests(unittest.TestCase):
    def test_studio_matches_by_alias(self) -> None:
        s = Studio(page_id="s1", name="Northlight Pictures", aliases=("NLP",))
        self.assertEqual(Lookup(Index(studios=(s,))).studio("nlp").entity, s)


class LlmStrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.a = Deliverable(page_id="e1", name="Ep 101 Final Master", project_id="p1")
        self.b = Deliverable(page_id="e2", name="Ep 102 Final Master", project_id="p1")
        self.index = Index(deliverables=(self.a, self.b))

    def test_without_a_judge_the_llm_strategy_is_skipped(self) -> None:
        r = Lookup(self.index).deliverable("Ep 10 Final Master", project_id="p1")
        self.assertEqual((r.entity, r.method, r.needs_review), (None, "review", True))

    def test_a_confident_judge_decides(self) -> None:
        seen: list[tuple[str, Sequence[str]]] = []

        def judge(mention: str, candidates: Sequence[NotionEntity]) -> Mapping[str, float] | None:
            seen.append((mention, [c.page_id for c in candidates]))
            return {"e1": 0.95, "e2": 0.03}

        r = Lookup(self.index, judge=judge).deliverable("Ep 10 Final Master", project_id="p1")
        self.assertEqual((r.entity, r.method, r.score, r.needs_review), (self.a, "llm", 0.95, False))
        self.assertEqual(len(seen), 1)

    def test_an_unsure_judge_leaves_it_to_a_human(self) -> None:
        judge = lambda mention, candidates: {"e1": 0.5, "e2": 0.45}  # noqa: E731
        r = Lookup(self.index, judge=judge).deliverable("Ep 10 Final Master", project_id="p1")
        self.assertEqual((r.entity, r.method, r.needs_review), (None, "review", True))

    def test_a_judge_with_no_answer_is_skipped(self) -> None:
        r = Lookup(self.index, judge=lambda m, c: None).deliverable("Ep 10 Final Master", project_id="p1")
        self.assertEqual(r.method, "review")

    def test_the_judge_is_not_called_when_a_cheaper_strategy_decides(self) -> None:
        def judge(mention: str, candidates: Sequence[NotionEntity]) -> Mapping[str, float] | None:
            raise AssertionError("must not be called")

        self.assertEqual(Lookup(self.index, judge=judge).deliverable("Ep 101 Final Master", project_id="p1").entity, self.a)

    def test_policy_can_turn_the_llm_off(self) -> None:
        judge = lambda m, c: {"e1": 0.99, "e2": 0.0}  # noqa: E731
        lookup = Lookup(self.index, judge=judge, policies={"deliverable": Policy(llm=False)})
        self.assertEqual(lookup.deliverable("Ep 10 Final Master", project_id="p1").method, "review")

    def test_jev_through_t6_llm_with_a_fake_provider(self) -> None:
        provider = FakeProvider()
        provider.decisions(
            {
                "model": "typesafe/jev-1.1",
                "answers": {
                    "c0": {"type": "choice", "choice": "match", "probabilities": {"match": 0.9, "maybe": 0.08, "no": 0.02}},
                    "c1": {"type": "choice", "choice": "no", "probabilities": {"match": 0.02, "maybe": 0.08, "no": 0.9}},
                },
            }
        )
        task: Task[dict[str, Withheld], CandidateDecisions] = Task(
            "lookup-deliverable",
            dict,
            CandidateDecisions,
            lambda payload: DecisionsRequest(
                state={"mention": payload["mention"].text},
                questions={
                    key: Choice("Is this the mentioned Deliverable?", {"match": "yes", "maybe": "unclear", "no": "no"})
                    for key in (candidate_key(0), candidate_key(1))
                },
            ),
            DataPolicy(masking="withhold"),
        )

        def judge(mention: str, candidates: Sequence[NotionEntity]) -> Mapping[str, float] | None:
            keys = CandidateKeys([c.page_id for c in candidates])
            result = run(task, {"mention": withhold(mention)}, provider=provider)
            if not isinstance(result, Ok):
                return None
            return {pid: d.probabilities.match for pid, d in keys.by_page_id(result.value).items()}

        r = Lookup(self.index, judge=judge).deliverable("Ep 10 Final Master", project_id="p1")
        self.assertEqual((r.entity, r.method), (self.a, "llm"))


class AliasTests(unittest.TestCase):
    def setUp(self) -> None:
        self.maria = contact("c1", "Maria Santos", aliases=("Mars",))
        self.lookup = Lookup(Index(contacts=(self.maria,)))

    def test_a_confirmed_alias_comes_back_as_a_proposed_write(self) -> None:
        proposal = self.lookup.propose_alias(self.maria, "@ate_mar")
        self.assertEqual(proposal, AliasWrite(database="Contacts", page_id="c1", property="Aliases", alias="ate_mar"))

    def test_nothing_is_proposed_for_a_name_the_entity_already_answers_to(self) -> None:
        self.assertIsNone(self.lookup.propose_alias(self.maria, "mars"))
        self.assertIsNone(self.lookup.propose_alias(self.maria, "Ate Maria Santos"))

    def test_studios_get_proposals_too(self) -> None:
        s = Studio(page_id="s1", name="Northlight Pictures")
        proposal = Lookup(Index()).propose_alias(s, "NLP")
        self.assertEqual(proposal and proposal.database, "Studios")

    def test_the_resolver_does_no_writes(self) -> None:
        fake = FakeNotion()
        fake.add_data_source("ds")
        lookup = Lookup(Index(contacts=(self.maria,)))
        lookup.propose_alias(self.maria, "new nickname")
        self.assertEqual(fake.page_count(), 0)
        self.assertEqual(fake.requests, [])


class ResolutionShapeTests(unittest.TestCase):
    def test_every_result_has_the_same_fields(self) -> None:
        lookup = Lookup(Index(machines=(Machine(page_id="m1", name="Bay", hostname="bay"),)))
        for r in (lookup.machine("bay"), lookup.machine("zzz"), lookup.contact("anyone")):
            self.assertEqual(
                [f.name for f in dataclasses.fields(r)],
                ["entity", "method", "score", "candidates", "needs_review", "reason"],
            )

    def test_results_are_json_friendly_to_log(self) -> None:
        r = Lookup(Index(machines=(Machine(page_id="m1", name="Bay", hostname="bay"),))).machine("bay")
        json.dumps({"method": r.method, "score": r.score, "needs_review": r.needs_review})
