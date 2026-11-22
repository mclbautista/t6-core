"""The preview, confirm and idempotency steps that every Preview-first write verb shares.

A verb that changes things first returns a Preview with a signed token, then on confirm does the work once. The
steps around that are the same for each verb, so they live here and the verbs (Media, Bay, Drive, Transcript) only
supply their own rules:

1. `begin`: check the idempotency key, name the request, check the token.
2. `settle`: inside the transaction, refuse a spent token or a key used for another request, and answer a retry
   from the stored first result.
3. `issue`: sign the Preview for the plan.
4. `finish`: spend the token and keep the result under the key, in the same transaction as the work.

The transcript verbs key their retries by phase (a preview and its confirm each keep their own result), so they use
the lower level `retry_of` and `keep` directly.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Any

from t6.preview import PreviewTokens
from t6.verbs import Done, Outcome, Preview, Refused

KEY_REQUIRED = 'A nonblank idempotency key is required'
KEY_REUSED = 'Idempotency key was used for a different request'
TOKEN_USED = 'Preview token has already been used'


def check_key(idempotency_key: str | None, confirm: str | None, reason: str = KEY_REQUIRED) -> Refused | None:
    """Refused unless the key is nonblank when given, and given whenever there is a confirm token."""
    if (idempotency_key is not None and not idempotency_key.strip()) or (confirm and not idempotency_key):
        return Refused(rule='idempotency_key', reason=reason)
    return None


def fingerprint(name: str, actor: str, inputs: dict[str, Any]) -> str:
    return json.dumps({'verb': name, 'actor': actor, **inputs}, sort_keys=True, separators=(',', ':'))


def retry_of(db: sqlite3.Connection, key: str, print_: str, *, reused: str = KEY_REUSED) -> Done | Refused | None:
    """The first result kept under `key`, when `print_` is the same request; Refused when it is another; else None."""
    previous = db.execute('SELECT fingerprint, result FROM verb_requests WHERE idempotency_key=?', (key,)).fetchone()
    if previous is None:
        return None
    if previous[0] != print_:
        return Refused(rule='idempotency_key', reason=reused)
    return Done(result=json.loads(previous[1]))


def keep(db: sqlite3.Connection, key: str, print_: str, result: Any, *, ignore_repeat: bool = False) -> None:
    """Keep a result under its key, so a retry returns it instead of acting again."""
    db.execute(('INSERT OR IGNORE' if ignore_repeat else 'INSERT') + ' INTO verb_requests VALUES (?, ?, ?)', (key, print_, json.dumps(result)))


def token_spent(db: sqlite3.Connection, nonce: str) -> Refused | None:
    """Refused when the token with this nonce was already spent."""
    if db.execute('SELECT 1 FROM used_preview_tokens WHERE nonce=?', (nonce,)).fetchone():
        return Refused(rule='token_used', reason=TOKEN_USED)
    return None


def spend(db: sqlite3.Connection, nonce: str, *, ignore_repeat: bool = False) -> None:
    """Spend a token, so a second confirm with it is refused."""
    db.execute(('INSERT OR IGNORE' if ignore_repeat else 'INSERT') + ' INTO used_preview_tokens VALUES (?)', (nonce,))


@dataclass(frozen=True)
class Request:
    """One call of a verb: who, what it is called, its key, the inputs the token covers, and the verified token's nonce."""
    name: str
    actor: str
    key: str
    inputs: dict[str, Any]
    print_: str
    nonce: str | None


def begin(tokens: PreviewTokens, name: str, actor: str, values: dict[str, Any], idempotency_key: str | None, confirm: str | None,
          *, key_reason: str = KEY_REQUIRED) -> Request | Refused:
    """Check the key and the confirm token; Refused names the rule that failed. A missing key is made up, so a preview needs none."""
    refusal = check_key(idempotency_key, confirm, key_reason)
    if refusal:
        return refusal
    key = idempotency_key or secrets.token_hex(16)
    inputs = {**values, 'idempotency_key': key}
    nonce = tokens.verify(confirm, name, actor, inputs) if confirm else None
    if isinstance(nonce, Refused):
        return nonce
    return Request(name, actor, key, inputs, fingerprint(name, actor, inputs), nonce)


def settle(db: sqlite3.Connection, tokens: PreviewTokens, request: Request, *, reused: str = KEY_REUSED,
           replay_spent: bool = False) -> Outcome | None:
    """The answer when the request needs no work: a spent token, a key used for another request, or a retry.

    A retry with its token spends the token and returns the first result; a retried preview returns a new Preview of
    the first result. None means the verb goes on to plan and act. With `replay_spent` a retry is answered before the
    token is checked, so a confirm retried with its token already spent still gets the first result.
    """
    if request.nonce and not replay_spent:
        spent = token_spent(db, request.nonce)
        if spent:
            return spent
    previous = retry_of(db, request.key, request.print_, reused=reused)
    if previous is None or isinstance(previous, Refused):
        if previous is None and request.nonce and replay_spent:
            return token_spent(db, request.nonce)
        return previous
    if request.nonce:
        spend(db, request.nonce, ignore_repeat=replay_spent)
        return previous
    return tokens.create(request.name, request.actor, request.inputs, previous.result)


def issue(tokens: PreviewTokens, request: Request, plan: Any) -> tuple[Preview, str]:
    """Sign the Preview of a plan; returns it with the token's nonce, which the verb keys its saved snapshot by."""
    preview = tokens.create(request.name, request.actor, request.inputs, plan)
    nonce = tokens.verify(preview.token, request.name, request.actor, request.inputs)
    assert isinstance(nonce, str)
    return preview, nonce


def finish(db: sqlite3.Connection, request: Request, result: Any) -> Done:
    """Spend the token and keep the result under the key; call it in the transaction that did the work."""
    assert request.nonce
    spend(db, request.nonce)
    keep(db, request.key, request.print_, result)
    return Done(result=result)
