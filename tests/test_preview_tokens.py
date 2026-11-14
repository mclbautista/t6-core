import unittest

from t6.preview import PreviewTokens
from t6.verbs import Refused


class PreviewTokenTests(unittest.TestCase):
    def test_signed_token_binds_request_and_expires(self) -> None:
        now = [100.0]
        tokens = PreviewTokens('secret', clock=lambda: now[0])
        preview = tokens.create('project hold', 'alice', {'project': 'p', 'reason': 'pause'}, 'Hold p')
        self.assertIsInstance(tokens.verify(preview.token, 'project hold', 'alice', {'reason': 'pause', 'project': 'p'}), str)
        for verb, actor, inputs in [
            ('project resume', 'alice', {'project': 'p', 'reason': 'pause'}),
            ('project hold', 'bob', {'project': 'p', 'reason': 'pause'}),
            ('project hold', 'alice', {'project': 'p', 'reason': 'changed'}),
        ]:
            result = tokens.verify(preview.token, verb, actor, inputs)
            assert isinstance(result, Refused)
            self.assertEqual(result.rule, 'token_mismatch')
        now[0] += 900
        result = tokens.verify(preview.token, 'project hold', 'alice', {'project': 'p', 'reason': 'pause'})
        assert isinstance(result, Refused)
        self.assertEqual(result.rule, 'token_expired')

    def test_tampered_token_is_refused(self) -> None:
        tokens = PreviewTokens('secret')
        preview = tokens.create('v', 'a', {}, 'plan')
        result = tokens.verify(preview.token + 'x', 'v', 'a', {})
        assert isinstance(result, Refused)
        self.assertEqual(result.rule, 'token_invalid')
