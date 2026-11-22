"""The one builder of the Notion-backed names and emails the three Google handlers share (#560)."""
import unittest

from t6.google.names import NotionNames, notion_names
from test_person import MemoryRegistry


class NotionNamesBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = MemoryRegistry()
        self.addCleanup(self.registry.db.close)

    def test_a_missing_token_names_the_variable_and_the_feature_that_needs_it(self) -> None:
        for reason in ('the calendar events carry Project and Person names', 'calendar sharing maps a Person to a Google email'):
            with self.assertRaises(ValueError) as caught:
                notion_names({}, self.registry, reason)
            self.assertEqual(str(caught.exception), f'missing environment variable: T6_NOTION_TOKEN ({reason})')

    def test_an_empty_token_counts_as_missing(self) -> None:
        with self.assertRaisesRegex(ValueError, 'T6_NOTION_TOKEN'):
            notion_names({'T6_NOTION_TOKEN': ''}, self.registry, 'a reason')

    def test_a_token_gives_the_names_object(self) -> None:
        self.assertIsInstance(notion_names({'T6_NOTION_TOKEN': 'token'}, self.registry, 'a reason'), NotionNames)


if __name__ == '__main__':
    unittest.main()
