"""Transcribe queue work; all state reports go through the caller's API."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from t6.transcribe.folder import FolderPreview, preview_folder, run_folder
from t6.transcribe.srt import _offset_seconds
from t6.worker.client import WorkerJob
from t6.worker.client import QueueClient, Transport
from t6.domain.events import JsonValue


class TranscriptWorker:
    def __init__(self, client: Any, report: Callable[[WorkerJob, str, dict[str, Any]], None]) -> None:
        self.client, self.report = client, report

    def _plan(self, job: WorkerJob) -> tuple[FolderPreview, dict[str, Any]]:
        data: dict[str, Any] = dict(job.payload)
        root = Path(str(data['audio_folder'])).resolve(strict=True)
        output_path = root / '_transcripts'
        output = output_path.resolve()
        if output_path.is_symlink() or not output.is_relative_to(root) or output == root:
            raise ValueError('Transcript output escapes audio folder')
        if data.get('start_timecode') is not None:
            _offset_seconds(str(data['start_timecode']), 24)
        manifest = data.get('manifest')
        if manifest:
            path = (root / str(manifest)).resolve(strict=True)
            if not path.is_relative_to(root):
                raise ValueError('Manifest escapes audio folder')
            manifest = str(path)
        formats = data.get('formats', ['srt', 'docx'])
        if not isinstance(formats, list) or not all(isinstance(f, str) for f in formats):
            raise ValueError('Invalid formats')
        cap = data.get('max_hours')
        plan = preview_folder(root, manifest=manifest, formats=tuple(formats), rate=float(data.get('rate', .4)), max_hours=float(cap) if cap is not None else None)
        digest = hashlib.sha256()
        if manifest:
            digest.update(Path(manifest).read_bytes())
        for clip in plan.clips:
            path = root / clip.source
            digest.update(json.dumps([str(clip.source), clip.seconds, clip.done], separators=(',', ':')).encode())
            with path.open('rb') as audio:
                for block in iter(lambda: audio.read(1024 * 1024), b''):
                    digest.update(block)
        result = {'files_found': plan.found, 'already_done': plan.already_done,
                  'hours_pending': plan.pending_hours, 'estimated_cost': plan.estimated_cost,
                  'snapshot': digest.hexdigest()}
        return plan, result

    def preview(self, job: WorkerJob) -> None:
        _, plan = self._plan(job)
        self.report(job, 'preview', plan)

    def run(self, job: WorkerJob) -> None:
        from t6.transcribe.adapter import transcribe
        plan, current = self._plan(job)
        if current['snapshot'] != job.payload.get('snapshot'):
            self.report(job, 'failed', {'rule': 'input_changed'})
            return
        counts = {'completed': 0, 'failed': 0}
        def progress(event: dict[str, Any]) -> None:
            counts.update(completed=event['completed'], failed=event['failed'])
            event['current_clip'] = str(event['current_clip'])
            self.report(job, 'progress', event)
        run_folder(plan, lambda path: transcribe(path, self.client, language=str(job.payload.get('language', 'fil')), diarize=not bool(job.payload.get('no_diarize', False))),
                   progress=progress, offset=str(job.payload.get('start_timecode') or '00:00:00:00'))
        self.report(job, 'failed' if counts['failed'] else 'complete', {'clips_done': counts['completed'], 'failed': counts['failed'],
                                      'skipped': plan.already_done, 'audio_hours': plan.pending_hours,
                                      'estimated_cost': plan.estimated_cost})

    def _speakers_plan(self, job: WorkerJob) -> tuple[list[tuple[Path, str, dict[str, Any]]], dict[str, Any]]:
        from t6.transcribe.folder import _load_transcript, _output
        from t6.transcribe.speakers import parse_speaker_map, speaker_name
        root = Path(str(job.payload['audio_folder'])).resolve(strict=True)
        output = root / '_transcripts'
        if output.is_symlink() or not output.resolve().is_relative_to(root):
            raise ValueError('Transcript output escapes audio folder')
        new = parse_speaker_map(str(job.payload['csv']))
        old = parse_speaker_map(str(job.payload['previous_map'])) if job.payload.get('previous_map') else {}
        digest = hashlib.sha256(str(job.payload.get('previous_map', '')).encode())
        clips = []
        changes = []
        for audio in sorted(root.rglob('*')):
            if not audio.is_file() or audio.suffix.lower() != '.wav' or not audio.resolve().is_relative_to(root) or output in audio.parents:
                continue
            source = str(audio.relative_to(root))
            path = _output(output, Path(source), '.json')
            if not path.exists():
                continue
            transcript = _load_transcript(path)
            if transcript is None:
                raise ValueError('Invalid stored transcript')
            digest.update(source.encode())
            digest.update(path.read_bytes())
            clips.append((path, source, transcript))
            speakers = sorted({word.get('speaker_id') for word in transcript['words'] if word.get('speaker_id')})
            for speaker in speakers:
                before, after = speaker_name(speaker, source, old), speaker_name(speaker, source, new)
                if before != after:
                    changes.append({'source': source, 'speaker': speaker, 'from': before, 'to': after})
        return clips, {'changes': changes, 'snapshot': digest.hexdigest()}

    def speakers_preview(self, job: WorkerJob) -> None:
        _, plan = self._speakers_plan(job)
        self.report(job, 'preview', plan)

    def speakers_run(self, job: WorkerJob) -> None:
        import tempfile
        from t6.transcribe.docx import render_docx
        from t6.transcribe.folder import _output, _valid_output, _write
        from t6.transcribe.speakers import parse_speaker_map
        from t6.transcribe.srt import render_srt
        clips, plan = self._speakers_plan(job)
        if plan['snapshot'] != job.payload.get('snapshot'):
            self.report(job, 'failed', {'rule': 'input_changed'})
            return
        output = Path(str(job.payload['audio_folder'])).resolve() / '_transcripts'
        speaker_map = parse_speaker_map(str(job.payload['csv']))
        offset = str(job.payload.get('start_timecode') or '00:00:00:00')
        rendered = []
        with tempfile.TemporaryDirectory() as directory:
            for _, source, transcript in clips:
                for suffix, content in (('.srt', render_srt(transcript, speaker_map=speaker_map, source=source, offset=offset).encode()),
                                        ('.docx', render_docx(transcript, speaker_map=speaker_map, source=source, offset=offset))):
                    staged = Path(directory) / ('output' + suffix)
                    staged.write_bytes(content)
                    if not _valid_output(staged, suffix):
                        raise ValueError('Invalid rendered output')
                    rendered.append((_output(output, Path(source), suffix), content))
            for path, content in rendered:
                _write(output, path, content)
        self.report(job, 'complete', {})


class TranscribeQueue:
    """Use the API for all job claims and reports."""
    def __init__(self, transport: Transport, secret: str) -> None:
        self.client = QueueClient(transport, secret)

    def claim(self, queue: str, lease_seconds: float = 300.0) -> WorkerJob | None:
        data = self.client._call('POST', '/v1/transcribe/claim', {})['job']
        return None if data is None else WorkerJob(data['job_id'], queue, data['kind'], data['dedupe_key'], data['payload'], data['token'], data['attempt'])

    def ack(self, job: WorkerJob, result: JsonValue = None) -> None:
        pass  # Final reports finish the job atomically on the API.

    def extend(self, job: WorkerJob, lease_seconds: float = 300.0) -> None:
        self.client._call('POST', '/v1/transcribe/extend', {'job_id': job.job_id, 'token': job.token})

    def fail(self, job: WorkerJob, reason: str, retry_in: float | None = None) -> None:
        self.client._call('POST', '/v1/transcribe/fail', {'job_id': job.job_id, 'token': job.token})

    def report(self, job: WorkerJob, kind: str, data: dict[str, Any]) -> None:
        self.client._call('POST', '/v1/transcribe/report', {'job_id': job.job_id, 'token': job.token, 'kind': kind, 'data': data})


def configured(environ: Mapping[str, str], queue: TranscribeQueue) -> dict[str, Any]:
    key = environ.get('ELEVENLABS_API_KEY')
    if not key:
        raise ValueError('Transcribe worker key is required')
    from elevenlabs.client import ElevenLabs
    worker = TranscriptWorker(ElevenLabs(api_key=key), queue.report)
    return {'transcript.preview': worker.preview, 'transcript.run': worker.run, 'transcript.speakers.preview': worker.speakers_preview, 'transcript.speakers.run': worker.speakers_run}
