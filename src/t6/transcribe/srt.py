"""Render ElevenLabs word JSON as SRT without requiring the SDK."""

from typing import Any


def _offset_seconds(offset: float | str, fps: float) -> float:
    if isinstance(offset, str):
        parts = offset.split(":")
        if len(parts) != 4 or not all(part.isdigit() for part in parts):
            raise ValueError("offset must be HH:MM:SS:FF")
        hours, minutes, seconds, frames = map(int, parts)
        if minutes >= 60 or seconds >= 60 or frames >= fps:
            raise ValueError("invalid timecode offset")
        return hours * 3600 + minutes * 60 + seconds + frames / fps
    return float(offset)


def _timestamp(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    if milliseconds < 0:
        raise ValueError("cue time cannot be negative")
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{milliseconds:03}"


def render_srt(transcript: dict[str, Any], *, offset: float | str = 0, fps: float = 24) -> str:
    """Render timed words, wrapping at 42 characters and splitting by speaker.

    Timecodes are non-drop-frame and use 24 fps unless specified. Words with
    missing timestamps are skipped; spacing tokens provide word separation.
    """
    if fps <= 0:
        raise ValueError("fps must be positive")
    shift = _offset_seconds(offset, fps)
    cues: list[tuple[float, float, str]] = []
    lines: list[str] = []
    start = end = 0.0
    speaker: str | None = None
    pending_space = ""

    def flush() -> None:
        if lines:
            cues.append((start, end, "\n".join(lines)))
            lines.clear()

    for word in transcript.get("words", []):
        if word["type"] == "spacing":
            pending_space = " "
            continue
        if word["type"] != "word" or word.get("start") is None or word.get("end") is None:
            continue
        current_speaker = word.get("speaker_id")
        if lines and current_speaker != speaker:
            flush()
            pending_space = ""
        if not lines:
            start = word["start"]
            speaker = current_speaker
        end = word["end"]
        text = word["text"]
        # Break unusually long tokens too, so the limit is unconditional.
        while text:
            if not lines:
                lines.append("")
            prefix = pending_space if lines[-1] else ""
            available = 42 - len(lines[-1]) - len(prefix)
            if available <= 0 or (len(text) <= 42 and len(text) > available):
                lines.append("")
                continue
            lines[-1] += prefix + text[:available]
            text = text[available:]
            pending_space = ""
            if text:
                lines.append("")
    flush()
    return "".join(
        f"{i}\n{_timestamp(a + shift)} --> {_timestamp(b + shift)}\n{text}\n\n"
        for i, (a, b, text) in enumerate(cues, 1)
    )
