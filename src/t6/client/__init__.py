"""Typed HTTP client for the t6 API. No server adapters are imported."""
from __future__ import annotations

import json
import math
import os
import secrets
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any, cast
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, TypeAdapter
from t6.verbs import (
    Ambiguous, Done, NeedsPerson, Outcome, Preview, Refused,
    AbangRequestInput, AbangCancelInput, AbangAcceptInput,
    VersionAddInput, VersionLockInput, VersionFindInput, VersionGetInput,
    MediaLinkInput, MediaUnlinkInput, TimelineLinkInput, TimelineRestampInput, TimelineListInput, FrameioProjectInput, MediaListInput, MediaMigrateInput,
    TranscriptCreateInput, TranscriptGetInput, TranscriptSpeakersInput, TranscriptTranslateInput,
    DriveCaptureInput, DriveCaptureListInput, DriveCaptureReadInput, DriveReloadReportInput,
    DriveMappingProposeInput, DriveMappingApplyInput, DriveProjectsInput, DriveOverdueInput, MovementListInput, MovementCorrectInput,
    DriveReloadInput, DriveReceiveInput, DriveReleaseInput, DriveReturnInput, DriveRetireInput, DriveAssignInput, DriveUnassignInput,
    DriveAddInput, DriveEditInput, DrivePromoteSerialInput, DriveListInput, DriveGetInput, DriveFindInput, DriveCatalogSnapshotInput, DriveCatalogSnapshotsInput, DriveCatalogDiffInput, DriveCatalogAtInput, DriveShipmentsInput, DriveShipmentInput, DriveExceptionsInput,
    MachineEditInput, MachineListInput, MachineSyncInput,
    SwingAssignInput, SwingUnassignInput, SwingReturnInput, SwingOverdueInput,
    BayBookInput, BayListInput, BayDriftInput, BayScanListInput, BayScanReportInput, BookingMoveInput, BookingExtendInput, BookingCancelInput, BookingGetInput, BookingListInput,
    DeliverableDeliverInput, PackagePrepareInput, PackageCheckInput, PackageHandoffInput, PackageAcceptInput, PackageRejectInput, PackageListInput, PackageGetInput, QueueDismissInput, QueueFailedInput,
    ApprovalRequestInput, ApprovalGrantInput, ApprovalDenyInput,
    ConcernScanInput, ConcernObserveInput, ConcernCountsInput, ConcernListInput, ConcernChangesInput, ConcernForMessageInput, ConcernRecordMessageInput, ConcernGetInput, ConcernRaiseInput, ConcernNoteInput, ConcernResolveInput, ConcernReopenInput,
    JoListInput, JoFindInput, DeliverableListInput, DeliverableCandidatesInput, FloorReadInput, DeliverableGetInput, DeliverableDefineInput, DeliverableEditInput, DeliverableRebindInput, DeliverableMissInput, DeliverableDelayInput, MilestoneRecordInput, MeetingNotesListInput, MeetingNotesCreateInput, MeetingNotesAppendInput, MeetingNotesTrashInput, MeetingNotesMarkSentInput, ProjectListInput, StudioNoticesInput, ExternalLinkFindInput, PersonListInput, ProjectGetInput, ProjectNotesInput, ProjectNotesHistoryInput, ProjectFlagsInput, StandingCcInput, TextWithholdInput, ActivityInput, CameraEnrichInput, CameraGetInput, DepartmentsInput,
    JoSubmitInput, JoCloseInput, JoBlockInput, JoUnblockInput, JoDelayInput, JoCancelInput,
    JoAssignInput, JoFormatInput, JoGetInput, JoNoteInput, JoRaiseInput, JoReassignInput,
    LLMDecideInput, LLMFilterInput, LLMRunInput, PersonAddInput, PersonCreateInput, PersonFindInput,
    PersonGetInput, PersonProjectRoleInput, PersonPermissionInput, PersonRebindInput, PersonShiftInput, PersonClassifyInput,
    PersonTelegramInput, ProjectChatInput, ProjectHoldInput, ProjectResolveInput, ProjectResumeInput, ProjectStartInput,
)


# A `concern scan` makes one llm run call per new PM Notes line inside one request, so 10 s is too short (#596).
SLOW_VERB_SECONDS = {('concern', 'scan'): 600.0}


def cli_timeout(noun: str, verb: str, environ: Mapping[str, str] = os.environ) -> float:
    """Seconds the CLI waits for the API: T6_API_TIMEOUT_SECONDS when set, else 10 (600 for `concern scan`)."""
    text = environ.get('T6_API_TIMEOUT_SECONDS', '').strip()
    if not text:
        return SLOW_VERB_SECONDS.get((noun, verb), 10.0)
    try:
        seconds = float(text)
    except ValueError:
        seconds = 0.0
    if not (math.isfinite(seconds) and seconds > 0):
        raise SystemExit(f'T6_API_TIMEOUT_SECONDS must be a positive number of seconds, not {text!r}')
    return seconds


@dataclass(frozen=True)
class Unavailable:
    """Transport failed; retain the key and inputs for a safe retry."""
    reason: str
    idempotency_key: str | None = None


class APIError(Exception):
    """The API returned a non-success HTTP response. `status` is its HTTP status, None when the request never went out."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


Result = Outcome | Unavailable
_outcome: TypeAdapter[Outcome] = TypeAdapter(Outcome)


class Client:
    def transcript_create(self, inputs: TranscriptCreateInput) -> Result:
        return self.call('transcript', 'create', inputs)

    def transcript_speakers(self, inputs: TranscriptSpeakersInput) -> Result:
        return self.call('transcript', 'speakers', inputs)

    def transcript_translate(self, inputs: TranscriptTranslateInput) -> Result:
        return self.call('transcript', 'translate', inputs)

    def transcript_get(self, inputs: TranscriptGetInput) -> Result:
        return self.call('transcript', 'get', inputs)

    def __init__(self, base_url: str | None = None, secret: str | None = None, *, timeout: float = 10,
                 telegram_user_id: int | None = None, mcp: bool = False, require_unmasked: bool = False) -> None:
        """`require_unmasked`: every call is refused (rule `unmasked`) when this caller would read masked names."""
        self.base_url = (base_url or os.environ.get('T6_API_URL', '')).rstrip('/')
        self.secret = secret if secret is not None else os.environ.get('T6_API_SECRET', '')
        self.timeout = timeout
        self.telegram_user_id = telegram_user_id
        self.mcp = mcp
        self.require_unmasked = require_unmasked

    def _request(self, path: str, inputs: BaseModel | dict[str, Any] | None = None,
                 *, idempotency_key: str | None = None) -> Any:
        if not self.base_url:
            return Unavailable('Set T6_API_URL and T6_API_SECRET to use the t6 API',
                               idempotency_key or (inputs.get('idempotency_key') if isinstance(inputs, dict) else getattr(inputs, 'idempotency_key', None)))
        headers = {'Authorization': 'Bearer ' + self.secret}
        if self.mcp:
            headers['X-T6-MCP'] = '1'
        if self.require_unmasked:
            headers['X-T6-Require-Unmasked'] = '1'
        if self.telegram_user_id is not None:
            headers['X-Telegram-User-Id'] = str(self.telegram_user_id)
        data = None
        key = None
        if inputs is not None:
            payload = dict(inputs) if isinstance(inputs, dict) else inputs.model_dump(mode='json', exclude_none=True, by_alias=True)
            for name in ('data_sources', 'file'):
                value = getattr(inputs, name, None)
                if isinstance(value, Path):
                    try:
                        payload[name + '_contents'] = value.read_text(encoding='utf-8')
                    except OSError as error:
                        raise APIError(f'Cannot read {value}: {error}') from error
                    payload.pop(name, None)
            # Registry state belongs to the API host, not the caller.
            payload.pop('registry_db', None)
            key = idempotency_key or payload.get('idempotency_key')
            if key:
                headers['Idempotency-Key'] = key
            headers['Content-Type'] = 'application/json'
            data = json.dumps(payload).encode()
        request = Request(self.base_url + path, data=data, headers=headers)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as error:
            raise APIError(f'API returned HTTP {error.code}: {error.read().decode(errors="replace")}', error.code) from error
        except (URLError, TimeoutError, OSError) as error:
            reason = str(error)
            if isinstance(getattr(error, 'reason', error), TimeoutError):
                reason += f" after {self.timeout:g} s; raise {'T6_MCP_TIMEOUT_SECONDS' if self.mcp else 'T6_API_TIMEOUT_SECONDS'}"
            return Unavailable(reason, key)

    def call(self, noun: str, verb: str, inputs: BaseModel) -> Result:
        if 'idempotency_key' in type(inputs).model_fields and not getattr(inputs, 'idempotency_key', None):
            inputs = inputs.model_copy(update={'idempotency_key': secrets.token_hex(16)})
        if (noun, verb) in {('person', 'get'), ('person', 'find'), ('project', 'resolve'),
                            ('jo', 'get'), ('camera', 'get'), ('departments', '')}:
            from t6.verbs import VERBS
            model = next(v.input_model for v in VERBS if (v.noun, v.verb) == (noun, verb))
            inputs = model.model_validate(inputs.model_dump(exclude={'data_sources', 'registry_db'}))
        value = self._request('/v1/' + noun + ('/' + verb.replace(' ', '/') if verb else ''), inputs)
        return value if isinstance(value, Unavailable) else _outcome.validate_python(value)

    def health(self) -> dict[str, Any] | Unavailable:
        return cast(dict[str, Any] | Unavailable, self._request('/health'))

    def tap(self, payload: dict[str, Any], *, idempotency_key: str) -> dict[str, Any] | Unavailable:
        """Deliver a gateway tap with its retry key and configured Telegram identity."""
        return cast(dict[str, Any] | Unavailable, self._request('/tap', payload, idempotency_key=idempotency_key))

    def admin_media_migrate(self, inputs: MediaMigrateInput) -> Result:
        return self.call('admin/media', 'migrate', inputs)

    def drive_capture(self, inputs: DriveCaptureInput) -> Result:
        return self.call('drive', 'capture', inputs)

    def drive_capture_undo(self, inputs: DriveCaptureReadInput) -> Result:
        return self.call('drive', 'capture undo', inputs)

    def drive_capture_list(self, inputs: DriveCaptureListInput) -> Result:
        return self.call('drive', 'capture list', inputs)

    def drive_capture_photo(self, inputs: DriveCaptureReadInput) -> Result:
        return self.call('drive', 'capture photo', inputs)

    def drive_reload_report(self, inputs: DriveReloadReportInput) -> Result:
        return self.call('drive', 'reload report', inputs)

    def drive_capture_result(self, inputs: DriveCaptureReadInput) -> Result:
        return self.call('drive', 'capture result', inputs)

    def drive_projects(self, inputs: DriveProjectsInput) -> Result:
        return self.call('drive', 'projects', inputs)

    def drive_overdue(self, inputs: DriveOverdueInput) -> Result:
        return self.call('drive', 'overdue', inputs)

    def movement_list(self, inputs: MovementListInput) -> Result:
        return self.call('movement', 'list', inputs)

    def movement_correct(self, inputs: MovementCorrectInput) -> Result:
        return self.call('movement', 'correct', inputs)

    def drive_reload(self, inputs: DriveReloadInput) -> Result:
        return self.call('drive', 'reload', inputs)

    def drive_mapping_propose(self, inputs: DriveMappingProposeInput) -> Result:
        return self.call('drive', 'mapping propose', inputs)

    def drive_mapping_apply(self, inputs: DriveMappingApplyInput) -> Result:
        return self.call('drive', 'mapping apply', inputs)

    def drive_receive(self, inputs: DriveReceiveInput) -> Result:
        return self.call('drive', 'receive', inputs)

    def drive_release(self, inputs: DriveReleaseInput) -> Result:
        return self.call('drive', 'release', inputs)

    def drive_return(self, inputs: DriveReturnInput) -> Result:
        return self.call('drive', 'return', inputs)

    def drive_retire(self, inputs: DriveRetireInput) -> Result:
        return self.call('drive', 'retire', inputs)

    def drive_assign(self, inputs: DriveAssignInput) -> Result:
        return self.call('drive', 'assign', inputs)

    def drive_unassign(self, inputs: DriveUnassignInput) -> Result:
        return self.call('drive', 'unassign', inputs)

    def drive_add(self, inputs: DriveAddInput) -> Result:
        return self.call('drive', 'add', inputs)

    def drive_edit(self, inputs: DriveEditInput) -> Result:
        return self.call('drive', 'edit', inputs)

    def drive_promote_serial(self, inputs: DrivePromoteSerialInput) -> Result:
        return self.call('drive', 'promote-serial', inputs)

    def drive_list(self, inputs: DriveListInput) -> Result:
        return self.call('drive', 'list', inputs)

    def drive_get(self, inputs: DriveGetInput) -> Result:
        return self.call('drive', 'get', inputs)

    def drive_find(self, inputs: DriveFindInput) -> Result:
        return self.call('drive', 'find', inputs)

    def drive_catalog_snapshot(self, inputs: DriveCatalogSnapshotInput) -> Result:
        return self.call('drive', 'catalog-snapshot', inputs)

    def drive_catalog_snapshots(self, inputs: DriveCatalogSnapshotsInput) -> Result:
        return self.call('drive', 'catalog-snapshots', inputs)

    def drive_catalog_diff(self, inputs: DriveCatalogDiffInput) -> Result:
        return self.call('drive', 'catalog-diff', inputs)

    def drive_catalog_at(self, inputs: DriveCatalogAtInput) -> Result:
        return self.call('drive', 'catalog-at', inputs)

    def drive_shipments(self, inputs: DriveShipmentsInput) -> Result:
        return self.call('drive', 'shipments', inputs)

    def drive_shipment(self, inputs: DriveShipmentInput) -> Result:
        return self.call('drive', 'shipment', inputs)

    def drive_exceptions(self, inputs: DriveExceptionsInput) -> Result:
        return self.call('drive', 'exceptions', inputs)

    def swing_assign(self, inputs: SwingAssignInput) -> Result:
        return self.call('swing', 'assign', inputs)

    def swing_unassign(self, inputs: SwingUnassignInput) -> Result:
        return self.call('swing', 'unassign', inputs)

    def swing_return(self, inputs: SwingReturnInput) -> Result:
        return self.call('swing', 'return', inputs)

    def swing_overdue(self, inputs: SwingOverdueInput) -> Result:
        return self.call('swing', 'overdue', inputs)

    def media_link(self, inputs: MediaLinkInput) -> Result:
        return self.call('media', 'link', inputs)

    def media_unlink(self, inputs: MediaUnlinkInput) -> Result:
        return self.call('media', 'unlink', inputs)

    def frameio_project_link(self, inputs: FrameioProjectInput) -> Result:
        return self.call('frameio', 'project link', inputs)

    def frameio_project_unlink(self, inputs: FrameioProjectInput) -> Result:
        return self.call('frameio', 'project unlink', inputs)

    def timeline_link(self, inputs: TimelineLinkInput) -> Result:
        return self.call('timeline', 'link', inputs)

    def timeline_restamp(self, inputs: TimelineRestampInput) -> Result:
        return self.call('timeline', 'restamp', inputs)

    def timeline_list(self, inputs: TimelineListInput) -> Result:
        return self.call('timeline', 'list', inputs)

    def media_list(self, inputs: MediaListInput) -> Result:
        return self.call('media', 'list', inputs)

    def bay_book(self, inputs: BayBookInput) -> Result:
        return self.call('bay', 'book', inputs)

    def bay_list(self, inputs: BayListInput) -> Result:
        return self.call('bay', 'list', inputs)

    def bay_scan_report(self, inputs: BayScanReportInput) -> Result:
        return self.call('bay', 'scan report', inputs)

    def bay_scan_list(self, inputs: BayScanListInput) -> Result:
        return self.call('bay', 'scan list', inputs)

    def bay_drift(self, inputs: BayDriftInput) -> Result:
        return self.call('bay', 'drift', inputs)

    def booking_move(self, inputs: BookingMoveInput) -> Result:
        return self.call('booking', 'move', inputs)

    def booking_extend(self, inputs: BookingExtendInput) -> Result:
        return self.call('booking', 'extend', inputs)

    def booking_cancel(self, inputs: BookingCancelInput) -> Result:
        return self.call('booking', 'cancel', inputs)

    def booking_get(self, inputs: BookingGetInput) -> Result:
        return self.call('booking', 'get', inputs)

    def booking_list(self, inputs: BookingListInput) -> Result:
        return self.call('booking', 'list', inputs)

    def llm_run(self, inputs: LLMRunInput) -> Result:
        return self.call('llm', 'run', inputs)

    def llm_decide(self, inputs: LLMDecideInput) -> Result:
        return self.call('llm', 'decide', inputs)

    def llm_filter(self, inputs: LLMFilterInput) -> Result:
        return self.call('llm', 'filter', inputs)

    def person_add(self, inputs: PersonAddInput) -> Result:
        return self.call('person', 'add', inputs)

    def person_create(self, inputs: PersonCreateInput) -> Result:
        return self.call('person', 'create', inputs)

    def machine_list(self, inputs: MachineListInput) -> Result:
        return self.call('machine', 'list', inputs)

    def machine_sync(self, inputs: MachineSyncInput) -> Result:
        return self.call('machine', 'sync', inputs)

    def machine_edit(self, inputs: MachineEditInput) -> Result:
        return self.call('machine', 'edit', inputs)

    def person_rebind(self, inputs: PersonRebindInput) -> Result:
        return self.call('person', 'rebind', inputs)

    def approval_request(self, inputs: ApprovalRequestInput) -> Result:
        return self.call('approval', 'request', inputs)

    def approval_grant(self, inputs: ApprovalGrantInput) -> Result:
        return self.call('approval', 'grant', inputs)

    def approval_deny(self, inputs: ApprovalDenyInput) -> Result:
        return self.call('approval', 'deny', inputs)

    def person_set_telegram(self, inputs: PersonTelegramInput) -> Result:
        return self.call('person', 'set-telegram', inputs)

    def person_shift(self, inputs: PersonShiftInput) -> Result:
        return self.call('person', 'shift', inputs)

    def person_classify(self, inputs: PersonClassifyInput) -> Result:
        return self.call('person', 'classify', inputs)

    def person_grant(self, inputs: PersonPermissionInput) -> Result:
        return self.call('person', 'grant', inputs)

    def person_revoke(self, inputs: PersonPermissionInput) -> Result:
        return self.call('person', 'revoke', inputs)

    def person_assign(self, inputs: PersonProjectRoleInput) -> Result:
        return self.call('person', 'assign', inputs)

    def person_unassign(self, inputs: PersonProjectRoleInput) -> Result:
        return self.call('person', 'unassign', inputs)

    def person_get(self, inputs: PersonGetInput) -> Result:
        return self.call('person', 'get', inputs)

    def studio_notices(self, inputs: StudioNoticesInput) -> Result:
        return self.call('studio', 'notices', inputs)

    def external_link_find(self, inputs: ExternalLinkFindInput) -> Result:
        return self.call('external-link', 'find', inputs)

    def concern_scan(self, inputs: ConcernScanInput) -> Result:
        return self.call('concern', 'scan', inputs)

    def concern_observe(self, inputs: ConcernObserveInput) -> Result:
        return self.call('concern', 'observe', inputs)

    def concern_counts(self, inputs: ConcernCountsInput) -> Result:
        return self.call('concern', 'counts', inputs)

    def concern_list(self, inputs: ConcernListInput) -> Result:
        return self.call('concern', 'list', inputs)

    def concern_changes(self, inputs: ConcernChangesInput) -> Result:
        return self.call('concern', 'changes', inputs)

    def project_notes(self, inputs: ProjectNotesInput) -> Result:
        return self.call('project', 'notes', inputs)

    def project_notes_history(self, inputs: ProjectNotesHistoryInput) -> Result:
        return self.call('project', 'notes-history', inputs)

    def project_get(self, inputs: ProjectGetInput) -> Result:
        return self.call('project', 'get', inputs)

    def project_flags(self, inputs: ProjectFlagsInput) -> Result:
        return self.call('project', 'flags', inputs)

    def person_list(self, inputs: PersonListInput) -> Result:
        return self.call('person', 'list', inputs)

    def text_withhold(self, inputs: TextWithholdInput) -> Result:
        return self.call('text', 'withhold', inputs)

    def standing_cc(self, inputs: StandingCcInput) -> Result:
        return self.call('standing-cc', '', inputs)

    def jo_list(self, inputs: JoListInput) -> Result:
        return self.call('jo', 'list', inputs)

    def jo_find(self, inputs: JoFindInput) -> Result:
        return self.call('jo', 'find', inputs)

    def abang_request(self, inputs: AbangRequestInput) -> Result:
        return self.call('abang', 'request', inputs)

    def abang_accept(self, inputs: AbangAcceptInput) -> Result:
        return self.call('abang', 'accept', inputs)

    def abang_cancel(self, inputs: AbangCancelInput) -> Result:
        return self.call('abang', 'cancel', inputs)

    def version_add(self, inputs: VersionAddInput) -> Result:
        return self.call('version', 'add', inputs)

    def version_lock(self, inputs: VersionLockInput) -> Result:
        return self.call('version', 'lock', inputs)

    def version_get(self, inputs: VersionGetInput) -> Result:
        return self.call('version', 'get', inputs)

    def version_find(self, inputs: VersionFindInput) -> Result:
        return self.call('version', 'find', inputs)

    def deliverable_milestone_record(self, inputs: MilestoneRecordInput) -> Result:
        return self.call('deliverable', 'milestone record', inputs)

    def deliverable_miss(self, inputs: DeliverableMissInput) -> Result:
        return self.call('deliverable', 'miss', inputs)

    def deliverable_delay(self, inputs: DeliverableDelayInput) -> Result:
        return self.call('deliverable', 'delay', inputs)

    def deliverable_deliver(self, inputs: DeliverableDeliverInput) -> Result:
        return self.call("deliverable", "deliver", inputs)

    def deliverable_define(self, inputs: DeliverableDefineInput) -> Result:
        return self.call('deliverable', 'define', inputs)

    def deliverable_edit(self, inputs: DeliverableEditInput) -> Result:
        return self.call('deliverable', 'edit', inputs)

    def deliverable_rebind(self, inputs: DeliverableRebindInput) -> Result:
        return self.call('deliverable', 'rebind', inputs)

    def deliverable_get(self, inputs: DeliverableGetInput) -> Result:
        return self.call('deliverable', 'get', inputs)

    def deliverable_list(self, inputs: DeliverableListInput) -> Result:
        return self.call('deliverable', 'list', inputs)

    def deliverable_candidates(self, inputs: DeliverableCandidatesInput) -> Result:
        return self.call('deliverable', 'candidates', inputs)

    def deliverable_bangko(self, inputs: FloorReadInput) -> Result:
        return self.call('deliverable', 'bangko', inputs)

    def deliverable_hand_to_mouth(self, inputs: FloorReadInput) -> Result:
        return self.call('deliverable', 'hand-to-mouth', inputs)

    def deliverable_sabit(self, inputs: FloorReadInput) -> Result:
        return self.call('deliverable', 'sabit', inputs)

    def meeting_notes_list(self, inputs: MeetingNotesListInput) -> Result:
        return self.call('meeting-notes', 'list', inputs)

    def meeting_notes_create(self, inputs: MeetingNotesCreateInput) -> Result:
        return self.call('meeting-notes', 'create', inputs)

    def meeting_notes_append(self, inputs: MeetingNotesAppendInput) -> Result:
        return self.call('meeting-notes', 'append', inputs)

    def meeting_notes_trash(self, inputs: MeetingNotesTrashInput) -> Result:
        return self.call('meeting-notes', 'trash', inputs)

    def meeting_notes_mark_sent(self, inputs: MeetingNotesMarkSentInput) -> Result:
        return self.call('meeting-notes', 'mark-sent', inputs)

    def project_list(self, inputs: ProjectListInput) -> Result:
        return self.call('project', 'list', inputs)

    def project_link_chat(self, inputs: ProjectChatInput) -> Result:
        return self.call('project', 'link-chat', inputs)

    def project_unlink_chat(self, inputs: ProjectChatInput) -> Result:
        return self.call('project', 'unlink-chat', inputs)

    def project_archive(self, inputs: ProjectResumeInput) -> Result:
        return self.call('project', 'archive', inputs)

    def project_start(self, inputs: ProjectStartInput) -> Result:
        return self.call('project', 'start', inputs)

    def project_hold(self, inputs: ProjectHoldInput) -> Result:
        return self.call('project', 'hold', inputs)

    def project_resume(self, inputs: ProjectResumeInput) -> Result:
        return self.call('project', 'resume', inputs)

    def project_activity(self, inputs: ActivityInput) -> Result:
        return self.call('project', 'activity', inputs)

    def package_prepare(self, inputs: PackagePrepareInput) -> Result:
        return self.call("package", "prepare", inputs)

    def package_check(self, inputs: PackageCheckInput) -> Result:
        return self.call("package", "check", inputs)

    def queue_failed(self, inputs: QueueFailedInput) -> Result:
        return self.call('queue', 'failed', inputs)

    def queue_dismiss(self, inputs: QueueDismissInput) -> Result:
        return self.call('queue', 'dismiss', inputs)

    def package_handoff(self, inputs: PackageHandoffInput) -> Result:
        return self.call("package", "handoff", inputs)

    def package_accept(self, inputs: PackageAcceptInput) -> Result:
        return self.call("package", "accept", inputs)

    def package_reject(self, inputs: PackageRejectInput) -> Result:
        return self.call("package", "reject", inputs)

    def package_list(self, inputs: PackageListInput) -> Result:
        return self.call("package", "list", inputs)

    def package_get(self, inputs: PackageGetInput) -> Result:
        return self.call("package", "get", inputs)

    def concern_for_message(self, inputs: ConcernForMessageInput) -> Result:
        return self.call('concern', 'for-message', inputs)

    def concern_record_message(self, inputs: ConcernRecordMessageInput) -> Result:
        return self.call('concern', 'record-message', inputs)

    def concern_get(self, inputs: ConcernGetInput) -> Result:
        return self.call("concern", "get", inputs)

    def concern_raise(self, inputs: ConcernRaiseInput) -> Result:
        return self.call("concern", "raise", inputs)

    def concern_note(self, inputs: ConcernNoteInput) -> Result:
        return self.call("concern", "note", inputs)

    def concern_resolve(self, inputs: ConcernResolveInput) -> Result:
        return self.call("concern", "resolve", inputs)

    def concern_reopen(self, inputs: ConcernReopenInput) -> Result:
        return self.call("concern", "reopen", inputs)

    def jo_get(self, inputs: JoGetInput) -> Result:
        return self.call('jo', 'get', inputs)

    def jo_raise(self, inputs: JoRaiseInput) -> Result:
        return self.call('jo', 'raise', inputs)

    def jo_assign(self, inputs: JoAssignInput) -> Result:
        return self.call('jo', 'assign', inputs)

    def jo_reassign(self, inputs: JoReassignInput) -> Result:
        return self.call('jo', 'reassign', inputs)

    def jo_note(self, inputs: JoNoteInput) -> Result:
        return self.call('jo', 'note', inputs)

    def jo_submit(self, inputs: JoSubmitInput) -> Result:
        return self.call('jo', 'submit', inputs)

    def jo_close(self, inputs: JoCloseInput) -> Result:
        return self.call('jo', 'close', inputs)

    def jo_block(self, inputs: JoBlockInput) -> Result:
        return self.call('jo', 'block', inputs)

    def jo_unblock(self, inputs: JoUnblockInput) -> Result:
        return self.call('jo', 'unblock', inputs)

    def jo_delay(self, inputs: JoDelayInput) -> Result:
        return self.call('jo', 'delay', inputs)

    def jo_cancel(self, inputs: JoCancelInput) -> Result:
        return self.call('jo', 'cancel', inputs)

    def jo_format(self, inputs: JoFormatInput) -> Result:
        return self.call('jo', 'format', inputs)

    def person_find(self, inputs: PersonFindInput) -> Result:
        return self.call('person', 'find', inputs)

    def project_resolve(self, inputs: ProjectResolveInput) -> Result:
        return self.call('project', 'resolve', inputs)

    def camera_get(self, inputs: CameraGetInput) -> Result:
        return self.call('camera', 'get', inputs)

    def camera_enrich(self, inputs: CameraEnrichInput) -> Result:
        return self.call('camera', 'enrich', inputs)

    def departments(self, inputs: DepartmentsInput) -> Result:
        return self.call('departments', '', inputs)
