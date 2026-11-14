"""Parse and resolve speaker names for transcript renderers."""

import csv
import io


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


def speaker_name(speaker: str | None, source: str, speaker_map: SpeakerMap) -> str:
    """Resolve a clip-specific name before a run-wide name."""
    label = speaker or ""
    return speaker_map.get((source, label), speaker_map.get(("", label), label))
