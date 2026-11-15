"""Confirmed milestone templates and their status projections."""
import unittest

from t6.deliverable_templates import PROJECT_TYPES, TEMPLATES, stage, status


class TemplateTests(unittest.TestCase):
    def test_full_and_skipped_sequences(self) -> None:
        for template, pipeline in TEMPLATES.items():
            facts: list[dict[str, str]] = []
            self.assertEqual(status(template, facts), 'Staged')
            for milestone in pipeline:
                facts.append({'milestone': milestone})
                self.assertEqual(stage(template, facts)['position'], milestone)
            self.assertEqual(status(template, facts), 'Archived')
            self.assertEqual(stage(template, [{'milestone': pipeline[-2]}, {'milestone': pipeline[0]}])['position'], 'archiving')
            self.assertEqual(status(template, [{'milestone': pipeline[-2]}]), 'Archiving')
            if template != 'film':
                self.assertEqual(stage(template, facts)['tracks'], {})
                self.assertEqual(status(template, [{'milestone': pipeline[0]}]), 'In Progress')

    def test_film_status_mapping_and_tracks(self) -> None:
        for milestone, expected in [('ingested', 'Waiting for Lock'), ('synced', 'Waiting for Lock'),
                                    ('piclock', 'In Progress'), ('conform done', 'In Progress'),
                                    ('master approved', 'Mastering'), ('QC passed', 'QC Passed'),
                                    ('delivered', 'Delivered'), ('archiving', 'Archiving'), ('archived', 'Archived')]:
            self.assertEqual(status('film', [{'milestone': milestone}]), expected)
        self.assertEqual(stage('film', [{'milestone': 'color delivered'}])['tracks']['color'], 'complete')
        self.assertEqual(status('film', [{'milestone': 'color delivered'}]), 'Staged')

    def test_project_types_are_explicit(self) -> None:
        self.assertEqual(PROJECT_TYPES['Limited Series'], 'film')
        self.assertEqual(PROJECT_TYPES['Serial Reality'], 'daily')
        self.assertEqual(PROJECT_TYPES['Serial TV - Color'], 'color_only')
        self.assertEqual(PROJECT_TYPES['Docu Ad'], 'tvc')
        for project_type in ('Music Video', 'Operations', 'Vertical Shorts', 'Special Format', 'Film Festival', '', 'Sound Only'):
            self.assertNotIn(project_type, PROJECT_TYPES)
