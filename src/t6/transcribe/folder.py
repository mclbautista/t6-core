"""Preview and run a resumable folder of WAV transcriptions.

Call :func:`preview_folder` and present its estimate before passing the plan to
:func:`run_folder`. The built-in DOCX renderer can be overridden by the caller.
"""

import csv
import json
import math
import os
import subprocess
import tempfile
import time
import wave
import zipfile
from xml.etree import ElementTree
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .docx import render_docx
from .speakers import SpeakerMap
from .srt import render_srt


@dataclass(frozen=True)
class Clip:
    source: Path  # relative to the audio folder
    seconds: float
    done: bool


@dataclass(frozen=True)
class FolderPreview:
    audio_folder: Path
    output_folder: Path
    formats: tuple[str, ...]
    clips: tuple[Clip, ...]
    rate: float

    @property
    def found(self) -> int:
        return len(self.clips)

    @property
    def already_done(self) -> int:
        return sum(clip.done for clip in self.clips)

    @property
    def pending(self) -> int:
        return self.found - self.already_done

    @property
    def pending_seconds(self) -> float:
        return sum(clip.seconds for clip in self.clips if not clip.done)

    @property
    def pending_hours(self) -> float:
        return self.pending_seconds / 3600

    @property
    def estimated_cost(self) -> float:
        return self.pending_hours * self.rate


def _inside(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"Path escapes folder: {path}")
    return resolved


def _output(root: Path, source: Path, suffix: str) -> Path:
    path = root / source.with_suffix(suffix)
    _inside(path, root)
    return path


def _load_transcript(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or not isinstance(data.get('words'), list):
            return None
        for word in data['words']:
            if not isinstance(word, dict) or not isinstance(word.get('type'), str):
                return None
            if word['type'] == 'word':
                if not isinstance(word.get('text'), str):
                    return None
                for key in ('start', 'end'):
                    value = word.get(key)
                    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
                        return None
                if word.get('speaker_id') is not None and not isinstance(word['speaker_id'], str):
                    return None
        return data
    except (OSError, UnicodeError, ValueError):
        return None


def _valid_output(path: Path, suffix: str) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    if suffix == '.json':
        return _load_transcript(path) is not None
    if suffix == '.docx':
        try:
            with zipfile.ZipFile(path) as doc:
                if doc.testzip() is not None:
                    return False
                types = ElementTree.fromstring(doc.read('[Content_Types].xml'))
                rels = ElementTree.fromstring(doc.read('_rels/.rels'))
                document = ElementTree.fromstring(doc.read('word/document.xml'))
                ct = 'http://schemas.openxmlformats.org/package/2006/content-types'
                rel = 'http://schemas.openxmlformats.org/package/2006/relationships'
                w = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
                return (types.tag == f'{{{ct}}}Types'
                        and any(e.get('PartName') == '/word/document.xml' and e.get('ContentType') == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml' for e in types.findall(f'{{{ct}}}Override'))
                        and any(e.get('Extension') == 'rels' and e.get('ContentType') == 'application/vnd.openxmlformats-package.relationships+xml' for e in types.findall(f'{{{ct}}}Default'))
                        and rels.tag == f'{{{rel}}}Relationships'
                        and any(e.get('Type') == 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument' and e.get('Target') in ('word/document.xml', '/word/document.xml') and e.get('TargetMode') != 'External' and e.get('Id') for e in rels.findall(f'{{{rel}}}Relationship'))
                        and document.tag == f'{{{w}}}document' and document.find(f'{{{w}}}body') is not None)
        except (OSError, zipfile.BadZipFile, KeyError, ElementTree.ParseError):
            return False
    return True


def _duration(path: Path) -> float:
    try:
        with wave.open(str(path), 'rb') as wav:
            return wav.getnframes() / wav.getframerate()
    except (wave.Error, EOFError, OSError, ZeroDivisionError):
        try:
            result = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
                capture_output=True, text=True, check=True,
            )
            seconds = float(result.stdout.strip())
            if math.isfinite(seconds) and seconds >= 0:
                return seconds
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        raise ValueError(f'Unreadable duration: {path}') from None


def preview_folder(
    audio_folder: str | Path, *, manifest: str | Path | None = None,
    output_folder: str | Path | None = None, formats: tuple[str, ...] = ('srt',),
    rate: float = 0.40, max_hours: float | None = None,
) -> FolderPreview:
    """Select and measure all clips, refusing invalid inputs before a run."""
    root = Path(audio_folder).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('audio folder must be a directory')
    output = Path(output_folder).absolute() if output_folder is not None else root / '_transcripts'
    output = output.resolve()
    if not math.isfinite(rate) or rate < 0 or (max_hours is not None and (not math.isfinite(max_hours) or max_hours < 0)):
        raise ValueError('rate and max_hours must be non-negative and finite')
    if any(fmt not in ('srt', 'docx', 'json') for fmt in formats):
        raise ValueError('unknown output format')
    suffixes = tuple(dict.fromkeys((*formats, 'json')))
    if manifest is None:
        paths = []
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith('.') and Path(directory, d).resolve() != output)
            for name in sorted(files):
                if not name.startswith('.') and Path(name).suffix.lower() == '.wav':
                    paths.append(Path(directory, name).relative_to(root))
    else:
        with open(manifest, newline='', encoding='utf-8-sig') as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or 'wav' not in reader.fieldnames:
                raise ValueError('manifest requires a wav column')
            paths = []
            for row in reader:
                value = row.get('wav') or ''
                path = Path(value)
                if not value or path.is_absolute():
                    raise ValueError(f'Invalid manifest path: {value}')
                paths.append(path)
    clips = []
    seen: set[Path] = set()
    destinations: set[Path] = set()
    unreadable = []
    for relative in paths:
        source = _inside(root / relative, root)
        if not source.is_file() or source.suffix.lower() != '.wav':
            raise ValueError(f'Not a WAV file: {relative}')
        if source in seen:
            raise ValueError(f'Duplicate WAV: {relative}')
        seen.add(source)
        # Normalize lexical traversal while preserving the selected folder structure.
        relative = Path(os.path.normpath(relative))
        outputs = [_output(output, relative, '.' + fmt) for fmt in suffixes]
        for path in outputs:
            if path in destinations:
                raise ValueError(f'Output collision: {path}')
            destinations.add(path)
        try:
            seconds = _duration(source)
        except ValueError:
            unreadable.append(str(relative))
            continue
        marker = _output(output, relative, '.transcribing')
        clips.append(Clip(relative, seconds, not marker.exists() and all(_valid_output(p, '.' + f) for p, f in zip(outputs, suffixes))))
    if unreadable:
        raise ValueError('Unreadable durations: ' + ', '.join(unreadable))
    plan = FolderPreview(root, output, suffixes, tuple(clips), rate)
    if max_hours is not None and plan.pending_hours > max_hours:
        raise ValueError(f'Pending hours {plan.pending_hours:.3f} exceeds limit {max_hours:.3f}')
    return plan


def _write(root: Path, path: Path, data: bytes) -> None:
    _inside(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    _inside(path, root)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.transcribe-')
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
        _inside(path, root)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def run_folder(
    plan: FolderPreview, transcribe: Callable[[Path], dict[str, Any]], *,
    docx_renderer: Callable[[dict[str, Any]], bytes] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    speaker_map: SpeakerMap | None = None,
) -> None:
    """Run a previously previewed plan; report each completed or failed clip.

    The callback receives state, completed, failed, total, current_clip,
    processed_seconds and total_seconds. Interruptions propagate to the caller.
    """
    pending = [clip for clip in plan.clips if not clip.done]
    total_seconds = sum(clip.seconds for clip in pending)
    completed = failed = 0
    processed = 0.0
    log_path = plan.audio_folder / '_transcription_log.csv'
    _inside(log_path, plan.audio_folder)
    for clip in pending:
        started = time.monotonic()
        outputs: list[str] = []
        error = ''
        try:
            source = _inside(plan.audio_folder / clip.source, plan.audio_folder)
            # Recheck output paths before making a potentially paid call.
            paths = {fmt: _output(plan.output_folder, clip.source, '.' + fmt) for fmt in plan.formats}
            marker = _output(plan.output_folder, clip.source, '.transcribing')
            _write(plan.output_folder, marker, b'in progress')
            transcript = _load_transcript(paths['json'])
            if transcript is None:
                transcript = transcribe(source)
            data: dict[str, bytes] = {
                'json': json.dumps(transcript, ensure_ascii=False, indent=2).encode('utf-8')
            }
            if 'srt' in paths:
                data['srt'] = render_srt(transcript, speaker_map=speaker_map, source=str(clip.source)).encode('utf-8')
            if 'docx' in paths:
                data['docx'] = (docx_renderer(transcript) if docx_renderer is not None else
                                render_docx(transcript, speaker_map=speaker_map, source=str(clip.source)))
            # Validate the entire set before replacing any existing output.
            with tempfile.TemporaryDirectory() as directory:
                for fmt, content in data.items():
                    staged = Path(directory) / ('output.' + fmt)
                    staged.write_bytes(content)
                    if not _valid_output(staged, '.' + fmt):
                        raise ValueError('Renderer produced an empty or invalid output')
                for fmt, path in paths.items():
                    _write(plan.output_folder, path, data[fmt])
                    outputs.append(str(path))
            _inside(marker, plan.output_folder)
            marker.unlink()
            completed += 1
            state = 'completed'
        except Exception as exc:
            failed += 1
            state = 'failed'
            error = str(exc)
        processed += clip.seconds
        _inside(log_path, plan.audio_folder)
        exists = log_path.exists() and log_path.stat().st_size > 0
        with log_path.open('a', newline='', encoding='utf-8') as handle:
            writer = csv.writer(handle)
            if not exists:
                writer.writerow(['timestamp', 'source', 'audio seconds', 'status', 'wall seconds', 'outputs', 'error'])
            writer.writerow([datetime.now(timezone.utc).isoformat(), str(clip.source), clip.seconds,
                             state, time.monotonic() - started, '; '.join(outputs), error])
        if progress:
            progress(dict(state=state, completed=completed, failed=failed, total=len(pending),
                          current_clip=clip.source, processed_seconds=processed, total_seconds=total_seconds))
