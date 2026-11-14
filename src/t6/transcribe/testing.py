"""Scripted fake ElevenLabs client for transcription tests."""

from collections.abc import Sequence
from typing import Any


class FakeResponse:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        return self.data


class FakeElevenLabs:
    def __init__(self, outcomes: Sequence[dict[str, Any] | Exception]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []
        self.speech_to_text = self

    def convert(self, **kwargs: Any) -> FakeResponse:
        kwargs["file"] = kwargs["file"].read()
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)
