"""Review fixes for the CRM part of spec #705: one stage rule, one list of Lead relations, shared lookups."""
import tempfile
import unittest
from pathlib import Path
from typing import Any

from t6.domain import CrmStage
from t6.registry.sqlite import SqliteProjectRegistry


class StageRuleTests(unittest.TestCase):
    def test_the_recorded_stage_wins_and_in_progress_ends_the_crm_part(self) -> None:
        from t6.domain.project_rules import crm_stage, is_lead
        self.assertIs(crm_stage('Onboarding', 'Leads'), CrmStage.ONBOARDING)
        self.assertIs(crm_stage(None, '🤝 Proposal Made'), CrmStage.PROPOSAL_MADE)
        self.assertIsNone(crm_stage('In-Progress', 'Leads'))  # the CRM ended; a stale Status does not reopen it
        self.assertIsNone(crm_stage(None, 'Standby'))
        self.assertTrue(is_lead(CrmStage.NEGOTIATIONS))
        self.assertFalse(is_lead(CrmStage.LOST))
        self.assertFalse(is_lead(None))


class RegistryStageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._folder = tempfile.TemporaryDirectory()
        self.addCleanup(self._folder.cleanup)
        self.registry = SqliteProjectRegistry(str(Path(self._folder.name, 'registry.db')))

    def register(self, number: int) -> str:
        found = self.registry.register(actor='setup', idempotency_key=f'k{number}', notion_page_id=f'00000000000040008000{number:012d}')
        return str(found.project.id)  # type: ignore[union-attr]

    def test_the_newest_row_per_project_in_one_query_or_for_one_project(self) -> None:
        from t6.registry.sqlite import latest_stages
        first, second, third = self.register(1), self.register(2), self.register(3)
        with self.registry._transaction() as db:
            for project, at, stage in ((first, 1.0, 'Leads'), (first, 2.0, 'Onboarding'), (second, 3.0, 'Lost')):
                db.execute("INSERT INTO project_stage_history (project_id, at, actor, field, from_value, to_value, event) VALUES (?, ?, 'x', 'crm_stage', NULL, ?, ?)",
                           (project, at, stage, f'e{at}'))
            self.assertEqual(latest_stages(db), {first: ('Onboarding', 2.0), second: ('Lost', 3.0)})
            self.assertEqual(latest_stages(db, first), {first: ('Onboarding', 2.0)})
            self.assertNotIn(third, latest_stages(db, third))

    def test_the_stage_changes_lookup_costs_one_transaction_however_many_projects(self) -> None:
        from t6.crm.reminder import stage_changes
        ids = [self.register(number) for number in range(1, 4)]
        opened: list[int] = []
        real = self.registry._transaction

        def counted() -> Any:
            opened.append(1)
            return real()
        self.registry._transaction = counted  # type: ignore[method-assign]
        last = stage_changes(self.registry)
        self.assertTrue(all(last(project) is not None for project in ids))
        self.assertEqual(len(opened), 1)

    def test_the_active_notion_page_of_a_project(self) -> None:
        project = self.registry.get(self.register(1))
        assert project is not None
        self.assertEqual(project.notion_page, '00000000000040008000000000000001')


class SharedLookupTests(unittest.TestCase):
    def test_the_lead_relations_are_listed_once(self) -> None:
        from t6.notion.projection import CRM_RELATIONS
        self.assertEqual([label for _, label in CRM_RELATIONS], ['Lead Owner', 'Delegate Producer', 'Line Producer', 'Prod Coor/Supe', 'Studio'])

    def test_a_telegram_id_finds_the_active_person(self) -> None:
        from t6.person import People
        from t6.preview import PreviewTokens
        from test_person import MemoryRegistry
        registry = MemoryRegistry()
        self.addCleanup(registry.db.close)
        with registry._transaction() as db:
            db.execute("INSERT INTO persons VALUES ('ada', 'staff', '[]', '[]', 700000001, 0, 1)")
            db.execute("INSERT INTO persons VALUES ('old', 'staff', '[]', '[]', 700000002, 0, 0)")
        people = People(registry, PreviewTokens('test'))
        self.assertEqual((people.by_telegram(700000001), people.by_telegram(700000002), people.by_telegram(1)), ('ada', None, None))


if __name__ == '__main__':
    unittest.main()
