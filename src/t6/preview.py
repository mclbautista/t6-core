"""Signed preview tokens for studio writes.

Token verification is independent of storage. Callers must consume the token
and commit the write atomically; verification alone does not make it single-use.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable, Mapping
from typing import Any

from t6.verbs import Preview, Refused


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode('ascii').rstrip('=')


def _digest(inputs: Mapping[str, Any]) -> str:
    encoded = json.dumps(inputs, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


class PreviewTokens:
    """Create and verify tokens using a configured secret and clock."""

    def __init__(self, key: str, *, expiry: float = 900, clock: Callable[[], float] = time.time) -> None:
        if not key or expiry <= 0:
            raise ValueError('a signing key and positive expiry are required')
        self._key = key.encode()
        self._expiry = expiry
        self._clock = clock

    def create(self, verb: str, actor: str, inputs: Mapping[str, Any], plan: Any) -> Preview:
        if not verb or not actor:
            raise ValueError('verb and actor are required')
        payload = {'verb': verb, 'actor': actor, 'inputs': _digest(inputs),
                   'expires': self._clock() + self._expiry, 'nonce': secrets.token_urlsafe(24)}
        body = _encode(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode())
        signature = _encode(hmac.digest(self._key, body.encode(), 'sha256'))
        return Preview(plan=plan, token=f'{body}.{signature}')

    def verify(self, token: str, verb: str, actor: str, inputs: Mapping[str, Any]) -> str | Refused:
        """Return the unique token nonce, or a refusal naming the failed rule."""
        try:
            body, signature = token.split('.')
            expected = _encode(hmac.digest(self._key, body.encode('ascii'), 'sha256'))
            if not hmac.compare_digest(signature, expected):
                raise ValueError('invalid signature')
            payload = json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))
            if not isinstance(payload, dict) or not isinstance(payload.get('nonce'), str):
                raise ValueError('invalid payload')
            if not isinstance(payload.get('expires'), (int, float)):
                raise ValueError('invalid expiry')
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
            return Refused(rule='token_invalid', reason='Invalid preview token')
        if payload['expires'] <= self._clock():
            return Refused(rule='token_expired', reason='Preview token has expired')
        if payload.get('verb') != verb or payload.get('actor') != actor or payload.get('inputs') != _digest(inputs):
            return Refused(rule='token_mismatch', reason='Preview token does not match this request')
        nonce: str = payload['nonce']
        return nonce


def submit_preview_job(queue: Any, *, queue_name: str, kind: str, dedupe_key: str,
                       verb: str, actor: str, inputs: Mapping[str, Any]) -> int:
    """Queue worker-only preview work; the signing key is never put in the job."""
    submitted = queue.submit(queue_name, kind, dedupe_key,
                             {'verb': verb, 'actor': actor, 'inputs': dict(inputs)})
    return int(submitted.job_id)


def finish_preview_job(queue: Any, job: Any, tokens: PreviewTokens, plan: Any) -> bool:
    """Complete a claimed job with the same Preview shape as a synchronous call."""
    payload = job.payload
    preview = tokens.create(payload['verb'], payload['actor'], payload['inputs'], plan)
    return bool(queue.ack(job.job_id, job.token, preview.model_dump(mode='json')))
