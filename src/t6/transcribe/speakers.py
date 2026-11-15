"""Parse and resolve speaker names for transcript renderers."""

import csv
import io
import re


SpeakerMap = dict[tuple[str, str], str]


def parse_speaker_map(content: str) -> SpeakerMap:
    """Parse CSV text, rejecting malformed or duplicate rows with line numbers."""
    reader = csv.reader(io.StringIO(content, newline=""), strict=True)
    try:
        header = next(reader, None)
        if header is None or len(header) != len(set(header)) or not {"source", "speaker", "name"} <= set(header):
            raise ValueError("line 1: expected source,speaker,name columns")
        result: SpeakerMap = {}
        for row in reader:
            if len(row) != len(header):
                raise ValueError(f"line {reader.line_num}: wrong number of columns")
            values = dict(zip(header, row))
            key = (values["source"], values["speaker"])
            if not values["speaker"] or not values["name"]:
                raise ValueError(f"line {reader.line_num}: speaker and name are required")
            if key in result:
                raise ValueError(f"line {reader.line_num}: duplicate source and speaker")
            result[key] = values["name"]
        return result
    except csv.Error as error:
        raise ValueError(f"line {reader.line_num}: {error}") from error


RAW_SPEAKER_ID = re.compile(r"speaker_\d+")
"""The raw ids the speech-to-text service gives speakers (for example ``speaker_0``)."""


def unmapped_label(speaker: str | None) -> str:
    """The label a renderer writes for a speaker with no speaker map entry: the raw id."""
    return speaker or ""


def is_unmapped_label(name: str) -> bool:
    """True when ``name`` is a raw id that ``unmapped_label`` can produce. Readers of rendered SRT use this."""
    return RAW_SPEAKER_ID.fullmatch(name) is not None


def speaker_name(speaker: str | None, source: str, speaker_map: SpeakerMap) -> str:
    """Resolve a clip-specific name before a run-wide name; with no entry, the unmapped label."""
    label = unmapped_label(speaker)
    return speaker_map.get((source, label), speaker_map.get(("", label), label))
