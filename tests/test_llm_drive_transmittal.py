"""Synthetic Jev evals through the Decisions task and queued provider seam."""
import unittest

from t6.llm.drive_transmittal import classify_transmittal
from t6.llm.testing import FakeProvider


class TransmittalEvalTests(unittest.TestCase):
    def test_synthetic_cases_pass_and_wrong_verdicts_fail(self) -> None:
        from t6.llm.drive_transmittal_evals import CASES, OFFLINE_REPLIES, grade_transmittal
        self.assertEqual({case.id for case in CASES}, {
            'out-photo', 'in-shelf-photo', 'question', 'unrelated-caption', 'photo-no-caption', 'text-only-out',
        })
        for case in CASES:
            with self.subTest(case=case.id):
                self.assertEqual(case.provenance.kind, 'synthetic')
                self.assertTrue(case.expected_properties)
                post = case.masked_input
                assert isinstance(post, dict)
                text, has_photo = post['text'], post['has_photo']
                assert isinstance(text, str) and isinstance(has_photo, bool)
                result = classify_transmittal(text, has_photo, provider=FakeProvider().decisions(OFFLINE_REPLIES[case.id]))
                self.assertTrue(grade_transmittal(case, result).passed)
                wrong = 'not_transmittal' if result == 'transmittal' else 'transmittal'
                self.assertFalse(grade_transmittal(case, wrong).passed)
                self.assertFalse(grade_transmittal(case, {}).passed)
