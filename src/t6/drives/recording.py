"""Admission of recording-channel content under ADR 0009."""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RecordingInput:
    text: str
    photo: bytes


def recording_input(
    chat_id: int, text: str, photo: bytes, *, recording_chat_id: int | None,
) -> RecordingInput | None:
    """Admit content for capture and photo reading only from the configured chat."""
    if recording_chat_id is None or chat_id != recording_chat_id:
        return None
    return RecordingInput(text, photo)
