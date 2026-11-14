"""Render a transcript as an in-memory DOCX document."""

import io
import zipfile
from typing import Any
from xml.etree import ElementTree as ET

from .speakers import SpeakerMap, speaker_name
from .srt import _offset_seconds, _timestamp


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
ET.register_namespace("w", _W)


def render_docx(
    transcript: dict[str, Any], *, offset: float | str = 0, fps: float = 24,
    speaker_map: SpeakerMap | None = None, source: str = "",
) -> bytes:
    """Return a validated DOCX with one paragraph for each speaker turn."""
    if fps <= 0:
        raise ValueError("fps must be positive")
    shift = _offset_seconds(offset, fps)
    document = ET.Element(f"{{{_W}}}document")
    body = ET.SubElement(document, f"{{{_W}}}body")
    speaker: str | None = None
    start = 0.0
    words: list[str] = []
    pending_space = ""

    def flush() -> None:
        if words:
            name = speaker_name(speaker, source, speaker_map or {})
            paragraph = ET.SubElement(body, f"{{{_W}}}p")
            run = ET.SubElement(paragraph, f"{{{_W}}}r")
            text = ET.SubElement(run, f"{{{_W}}}t")
            text.text = f"{name} [{_timestamp(start + shift)}] {''.join(words)}"
            words.clear()

    for word in transcript.get("words", []):
        if word["type"] == "spacing":
            pending_space = " "
            continue
        if word["type"] != "word" or word.get("start") is None or word.get("end") is None:
            continue
        current_speaker = word.get("speaker_id")
        if words and current_speaker != speaker:
            flush()
            pending_space = ""
        if not words:
            speaker = current_speaker
            start = word["start"]
        words.append((pending_space if words else "") + word["text"])
        pending_space = ""
    flush()
    ET.SubElement(body, f"{{{_W}}}sectPr")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?>'
                         '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                         '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                         '<Default Extension="xml" ContentType="application/xml"/>'
                         '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                         '</Types>')
        archive.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?>'
                         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                         '</Relationships>')
        archive.writestr("word/document.xml", ET.tostring(document, encoding="utf-8", xml_declaration=True))
    data = output.getvalue()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if archive.testzip() is not None:
            raise ValueError("invalid DOCX archive")
        ET.fromstring(archive.read("word/document.xml"))
    return data
