"""The shared preview, confirm and idempotency steps, and the vocabulary every verb reads."""
import tempfile
import unittest
from typing import Any

from t6 import verbflow
from t6.preview import PreviewTokens
from t6.registry.sqlite import SqliteProjectRegistry
from t6.verbs import Done, Preview, Refused


class VerbFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.registry = SqliteProjectRegistry(directory.name + '/db')
        self.tokens = PreviewTokens('signing')

    def run_verb(self, values: dict[str, Any], key: str | None, confirm: str | None = None) -> Any:
        """A tiny verb: its plan is the values; confirming does one write and keeps it."""
        request = verbflow.begin(self.tokens, 'thing make', 'actor', values, key, confirm)
        if isinstance(request, Refused):
            return request
        with self.registry._transaction() as db:
            settled = verbflow.settle(db, self.tokens, request)
            if settled:
                return settled
            plan = {**values, 'idempotency_key': request.key}
            if not request.nonce:
                return verbflow.issue(self.tokens, request, plan)[0]
            return verbflow.finish(db, request, plan)

    def test_a_confirm_needs_a_key_and_a_given_key_must_not_be_blank(self) -> None:
        preview = self.run_verb({'n': 1}, 'k')
        assert isinstance(preview, Preview)
        self.assertEqual(self.run_verb({'n': 1}, None, preview.token).rule, 'idempotency_key')
        self.assertEqual(self.run_verb({'n': 1}, ' ').rule, 'idempotency_key')
        self.assertIsInstance(self.run_verb({'n': 1}, None), Preview)

    def test_confirm_is_once_only_and_a_new_token_under_the_same_key_returns_the_first_result(self) -> None:
        preview = self.run_verb({'n': 1}, 'k')
        assert isinstance(preview, Preview)
        first = self.run_verb({'n': 1}, 'k', preview.token)
        self.assertIsInstance(first, Done)
        again = self.run_verb({'n': 1}, 'k', preview.token)
        assert isinstance(again, Refused)
        self.assertEqual(again.rule, 'token_used')
        retried = self.run_verb({'n': 1}, 'k')
        assert isinstance(retried, Preview)
        self.assertEqual(self.run_verb({'n': 1}, 'k', retried.token), first)

    def test_a_key_used_for_another_request_is_refused(self) -> None:
        preview = self.run_verb({'n': 1}, 'k')
        assert isinstance(preview, Preview)
        self.run_verb({'n': 1}, 'k', preview.token)
        other = self.run_verb({'n': 2}, 'k')
        assert isinstance(other, Refused)
        self.assertEqual((other.rule, other.reason), ('idempotency_key', verbflow.KEY_REUSED))

    def test_a_retried_preview_after_the_work_is_a_preview_of_the_first_result(self) -> None:
        preview = self.run_verb({'n': 1}, 'k')
        assert isinstance(preview, Preview)
        self.run_verb({'n': 1}, 'k', preview.token)
        retry = self.run_verb({'n': 1}, 'k')
        assert isinstance(retry, Preview)
        self.assertEqual(retry.plan, {'n': 1, 'idempotency_key': 'k'})


class VocabularyTests(unittest.TestCase):
    def test_the_verb_schemas_and_the_stores_read_the_same_lists(self) -> None:
        from t6.drives import Role
        from t6.media import KINDS, ROLES
        from t6.verbs import catalogue
        from t6.vocabulary import DRIVE_ROLES, MEDIA_KINDS, MEDIA_ROLES
        entries = {(e['noun'], e['verb']): e['input_schema']['properties'] for e in catalogue()}
        self.assertEqual(tuple(entries[('media', 'link')]['kind']['enum']), MEDIA_KINDS)
        self.assertEqual(tuple(entries[('media', 'link')]['role']['enum']), MEDIA_ROLES)
        self.assertEqual(tuple(entries[('drive', 'assign')]['role']['enum']), DRIVE_ROLES)
        self.assertEqual((KINDS, ROLES), (set(MEDIA_KINDS), set(MEDIA_ROLES)))
        self.assertEqual(tuple(role.value for role in Role), DRIVE_ROLES)
