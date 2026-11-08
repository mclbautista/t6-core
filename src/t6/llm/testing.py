"""A scripted fake provider for tests. No network, no recorded live replies.

Queue what the provider should do, in order, then run tasks against it::

    fake = FakeProvider()
    fake.reply('{"summary": "[person 1] confirmed"}', prompt_tokens=40, completion_tokens=8)
    fake.reply("not json")
    fake.timeout()
    fake.api_error("HTTP 500")

Each call to ``complete`` takes the next scripted step. ``calls`` records the
messages sent, so a test can check that only masked text left the app.
"""

from __future__ import annotations

import dataclasses
from collections import deque
from collections.abc import Sequence

from t6.llm.result import Tokens
from t6.llm.transport import Completion, Message, ProviderError, ProviderTimeout


@dataclasses.dataclass(frozen=True)
class Call:
    messages: tuple[Message, ...]
    timeout: float


class FakeProvider:
    """Replays scripted replies and failures in order."""

    def __init__(self, model: str = "fake-model") -> None:
        self._model = model
        self._script: deque[Completion | Exception] = deque()
        self.calls: list[Call] = []

    @property
    def model(self) -> str:
        return self._model

    def reply(
        self,
        content: str | None,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        model: str | None = None,
    ) -> FakeProvider:
        """Queue a reply. ``content`` may be malformed JSON, empty, ``None`` or placeholder-mangled."""
        self._script.append(Completion(content, model or self._model, Tokens(prompt_tokens, completion_tokens)))
        return self

    def timeout(self) -> FakeProvider:
        """Queue a timeout."""
        self._script.append(ProviderTimeout("scripted timeout"))
        return self

    def api_error(self, detail: str = "scripted API error") -> FakeProvider:
        """Queue a provider error."""
        self._script.append(ProviderError(detail))
        return self

    @property
    def pending(self) -> int:
        """Scripted steps not used yet."""
        return len(self._script)

    def complete(self, messages: Sequence[Message], *, timeout: float) -> Completion:
        self.calls.append(Call(tuple(messages), timeout))
        if not self._script:
            raise AssertionError("FakeProvider has no scripted reply left")
        step = self._script.popleft()
        if isinstance(step, Exception):
            raise step
        return step
