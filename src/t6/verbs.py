"""Studio verb catalogue and outcomes.

Add a verb by defining its input/output models and handler, then adding one
Verb entry to VERBS. The CLI derives arguments from model fields (field names
become required --options), and the catalogue derives schemas from the models.
A handler takes its input model and a Notion client factory and returns an
outcome. The empty verb name preserves the existing ``t6 departments`` read.

Outcome JSON shapes and exit codes: done (0) {outcome, result}, preview (0)
{outcome, plan, token}, needs_person (3) {outcome, reason}, refused (2)
{outcome, rule, reason}, ambiguous (4) {outcome, candidates}. The departments
CLI retains its original result and error JSON for backwards compatibility.
"""
from __future__ import annotations

import argparse
from contextvars import ContextVar
import json
import os
from dataclasses import asdict, dataclass
from datetime import date as IsoDate, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal, get_args, get_origin
from types import UnionType

from pydantic import AwareDatetime, BaseModel, Field, ConfigDict, SerializerFunctionWrapHandler, field_validator, model_serializer, model_validator
from t6.domain import Project, JobOrder, Deliverable, MeetingNote, Contact, Camera, ExternalRef, Machine, Ring
from t6.bay_scan import BayScanReport
from t6.vocabulary import DRIVE_ROLES, DriveRole, MediaKind, MediaRole, PersonKind
from t6.llm.api import Done as Done, LLMDecideInput as LLMDecideInput, LLMDecideOutput as LLMDecideOutput, LLMFilterInput as LLMFilterInput, LLMFilterOutput as LLMFilterOutput, LLMRunInput as LLMRunInput, LLMRunOutput as LLMRunOutput

if TYPE_CHECKING:
    from t6.notion.source import NotionSource
    from t6.person import People


_actor: ContextVar[str] = ContextVar("verb_actor", default="")
_registry: ContextVar[Any] = ContextVar("verb_registry", default=None)
_frameio: ContextVar[Any] = ContextVar("verb_frameio", default=None)
"""The way to ask the Frame.io worker (`t6.frameio.project_folders.FrameioProjectWriter`), set by the API; None in a process that has none."""
_notion_source: ContextVar[Any] = ContextVar("verb_notion_source", default=None)


def _verb_notion_source(open_notion: Callable[..., Any], token: str) -> NotionSource:
    from t6.notion.source import NotionSource

    injected = _notion_source.get()
    if injected is not None:
        return injected  # type: ignore[no-any-return]
    return NotionSource(lambda: open_notion(token))


class Preview(BaseModel):
    outcome: Literal["preview"] = "preview"
    plan: Any
    token: str


class NeedsPerson(BaseModel):
    outcome: Literal["needs_person"] = "needs_person"
    reason: str


class Refused(BaseModel):
    outcome: Literal["refused"] = "refused"
    rule: str
    reason: str
    failure: str | None = None
    retry_after: float | None = None

    @model_serializer(mode="wrap")
    def _omit_unset_llm_fields(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        for key in ("failure", "retry_after"):
            if data.get(key) is None:
                data.pop(key, None)
        return data


class Ambiguous(BaseModel):
    outcome: Literal["ambiguous"] = "ambiguous"
    candidates: list[Any]


Outcome = Done | Preview | NeedsPerson | Refused | Ambiguous
EXIT_CODES = {Done: 0, Preview: 0, NeedsPerson: 3, Refused: 2, Ambiguous: 4}


def outcome_json(outcome: Outcome) -> tuple[int, dict[str, Any]]:
    """Return the documented exit code and JSON-compatible outcome."""
    return EXIT_CODES[type(outcome)], outcome.model_dump(mode="json")


class LegacyDepartmentsInput(BaseModel):
    data_sources: Path
    include_misc: bool = False


class Department(BaseModel):
    id: str
    name: str


class DepartmentsOutput(BaseModel):
    departments: list[Department]


def departments(inputs: LegacyDepartmentsInput, open_notion: Callable[..., Any]) -> Outcome:
    return Refused(rule="api", reason="Departments are read on the t6 API")


class ActivityInput(BaseModel):
    project: str
    since: str | None = None
    limit: int | None = None


class ActivityItem(BaseModel):
    project_id: str
    subject_id: str | None
    at: float
    actor: str
    action: str
    summary: str
    audit_id: int | None


class ActivityOutput(BaseModel):
    entries: list[ActivityItem]


def project_activity(inputs: ActivityInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.cli import DEFAULT_REGISTRY_DB, REGISTRY_DB_VARIABLE, _open_registry
    from t6.registry import RegistryError, normalize_code

    try:
        since = None
        if inputs.since is not None:
            parsed = datetime.fromisoformat(inputs.since.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            since = parsed.timestamp()
        if inputs.limit is not None and inputs.limit < 1:
            raise ValueError("limit must be positive")
        registry = _registry.get() or _open_registry(Path(os.environ.get(REGISTRY_DB_VARIABLE, DEFAULT_REGISTRY_DB)))
        if registry is None:
            return Refused(rule="registry", reason="Project registry is unavailable")
        project = registry.get(inputs.project)
        if project is None:
            _, form = normalize_code(inputs.project)
            matches = [p for p in registry.list_projects() if normalize_code(p.code)[1] == form]
            if len(matches) > 1:
                return Ambiguous(candidates=[p.id for p in matches])
            project = matches[0] if matches else None
        if project is None:
            return Refused(rule="project", reason="Project not found")
        return Done(result=ActivityOutput(entries=[ActivityItem(**asdict(item)) for item in registry.activity(
            project.id, since=since, limit=inputs.limit
        )]))
    except (ValueError, OSError, RegistryError) as error:
        return Refused(rule="input", reason=str(error))



class ProjectStartDeliverable(BaseModel):
    code: str
    episode: str | None = None
    due: str | None = None
    air: str | None = None


class ProjectStartMedia(BaseModel):
    kind: MediaKind
    ref: str
    role: MediaRole


class ProjectStartBooking(BaseModel):
    bay: str
    person: str
    start: str
    end: str | None = None
    shift: Literal['day', 'night'] | None = None
    force: bool = False


class ProjectStartInput(BaseModel):
    project: str
    deliverables: list[ProjectStartDeliverable] = Field(default_factory=list)
    media: list[ProjectStartMedia] = Field(default_factory=list)
    bookings: list[ProjectStartBooking] = Field(default_factory=list)
    job: int | None = None
    idempotency_key: str | None = None
    confirm: str | None = None


class ProjectStartOutput(BaseModel):
    job_id: int | None = None
    state: str | None = None
    project_id: str | None = None
    idempotency_key: str | None = None
    approval_id: str | None = None
    reason: str | None = None
    deliverable_ids: list[str] = Field(default_factory=list)
    frameio_project_id: str | None = None
    frameio_folders: dict[str, str] = Field(default_factory=dict)
    frameio_media_ids: list[str] = Field(default_factory=list)
    media_ids: list[str] = Field(default_factory=list)
    booking_ids: list[str] = Field(default_factory=list)
    manual_steps: list[str] = Field(default_factory=list)


def project_start(inputs: ProjectStartInput, open_notion: Callable[..., Any]) -> Outcome:
    """Queue the plan job, read its Preview (or the Start's state) with `job`, or request the Approval with `confirm`; the rules are in `t6.project_start`."""
    from t6.project_start import ProjectStartStore
    if not os.environ.get('T6_PREVIEW_SIGNING_KEY'):
        return Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
    people = _people()
    store = ProjectStartStore(people.registry, people.tokens)
    values = inputs.model_dump(exclude={'job', 'idempotency_key', 'confirm'})
    if inputs.job is not None:
        return store.job_result(inputs.job, actor=_actor.get())
    if inputs.confirm:
        return store.confirm(values, actor=_actor.get(), idempotency_key=inputs.idempotency_key, token=inputs.confirm, frameio=_frameio.get())
    return store.start(values, actor=_actor.get(), idempotency_key=inputs.idempotency_key)


class ProjectHoldInput(BaseModel):
    project: str
    reason: str
    idempotency_key: str | None = None
    confirm: str | None = None


class ProjectResumeInput(BaseModel):
    project: str
    idempotency_key: str | None = None
    confirm: str | None = None


class ProjectWriteOutput(BaseModel):
    project_id: str
    action: str
    reason: str | None
    idempotency_key: str
    status: str


def _project_write(inputs: ProjectHoldInput | ProjectResumeInput, verb: str) -> Outcome:
    from t6.cli import DEFAULT_REGISTRY_DB, REGISTRY_DB_VARIABLE, _open_registry
    from t6.preview import PreviewTokens
    from t6.project_writes import ProjectWrites
    from t6.registry import RegistryError

    key = os.environ.get("T6_PREVIEW_SIGNING_KEY", "")
    if not key:
        return Refused(rule="config", reason="Set T6_PREVIEW_SIGNING_KEY")
    registry = _registry.get() or _open_registry(Path(os.environ.get(REGISTRY_DB_VARIABLE, DEFAULT_REGISTRY_DB)))
    if registry is None:
        return Refused(rule="registry", reason="Project registry is unavailable")
    try:
        return ProjectWrites(registry, PreviewTokens(key)).run(
            verb, inputs.project, inputs.reason if isinstance(inputs, ProjectHoldInput) else None,
            actor=_actor.get(), idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)
    except (RegistryError, OSError) as error:
        return Refused(rule="registry", reason=str(error))


def project_archive(inputs: ProjectResumeInput, open_notion: Callable[..., Any]) -> Outcome:
    return _project_write(inputs, "archive")


def project_hold(inputs: ProjectHoldInput, open_notion: Callable[..., Any]) -> Outcome:
    return _project_write(inputs, "hold")


def project_resume(inputs: ProjectResumeInput, open_notion: Callable[..., Any]) -> Outcome:
    return _project_write(inputs, "resume")


class ProjectChatInput(BaseModel):
    project: str
    chat: int
    topic: int | None = None
    idempotency_key: str | None = None
    confirm: str | None = None


class ProjectChatOutput(BaseModel):
    project_id: str
    action: str
    chat: int
    topic: int | None
    state: str
    chat_link_id: str | None
    idempotency_key: str
    approval_id: str | None = None


def _project_chat(inputs: ProjectChatInput, verb: str) -> Outcome:
    from t6.chat_link import ChatLinks
    from t6.preview import PreviewTokens
    key = os.environ.get('T6_PREVIEW_SIGNING_KEY', '')
    if not key:
        return Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
    return ChatLinks(_people().registry, PreviewTokens(key)).write(
        verb, inputs.model_dump(exclude={'confirm', 'idempotency_key'}), actor=_actor.get(),
        idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)


def project_link_chat(inputs: ProjectChatInput, open_notion: Callable[..., Any]) -> Outcome:
    return _project_chat(inputs, 'link-chat')


def project_unlink_chat(inputs: ProjectChatInput, open_notion: Callable[..., Any]) -> Outcome:
    return _project_chat(inputs, 'unlink-chat')

class PersonAddInput(BaseModel):
    page_id: str
    kind: str
    departments: list[str] = []
    roles: list[str] = []
    telegram_id: int | None = None
    master_brief: bool = False
    active: bool = True
    idempotency_key: str | None = None
    confirm: str | None = None


class PersonCreateInput(BaseModel):
    name: str
    kind: PersonKind
    telegram_handle: str | None = None
    telegram_id: int | None = None
    note: str | None = None
    idempotency_key: str | None = None
    confirm: str | None = None


class PersonTelegramInput(BaseModel):
    person_id: str
    telegram_id: int
    idempotency_key: str | None = None
    confirm: str | None = None


class PersonRebindInput(BaseModel):
    person_id: str
    page_id: str
    idempotency_key: str | None = None
    confirm: str | None = None


class PersonShiftInput(BaseModel):
    person_id: str
    pattern: str
    start: str
    end: str
    idempotency_key: str | None = None
    confirm: str | None = None


class PersonProjectRoleInput(BaseModel):
    person_id: str
    project: str
    role: str
    idempotency_key: str | None = None
    confirm: str | None = None


def person_project_role(inputs: PersonProjectRoleInput, open_notion: Callable[..., Any], verb: str) -> Outcome:
    if not os.environ.get("T6_PREVIEW_SIGNING_KEY"):
        return Refused(rule="config", reason="Set T6_PREVIEW_SIGNING_KEY")
    return _people().write(verb, inputs.model_dump(exclude={"confirm", "idempotency_key"}), actor=_actor.get(),
                           idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)


def person_assign(inputs: PersonProjectRoleInput, open_notion: Callable[..., Any]) -> Outcome:
    return person_project_role(inputs, open_notion, 'assign')


def person_unassign(inputs: PersonProjectRoleInput, open_notion: Callable[..., Any]) -> Outcome:
    return person_project_role(inputs, open_notion, 'unassign')


class PersonClassifyInput(BaseModel):
    person_id: str
    kind: PersonKind
    idempotency_key: str | None = None
    confirm: str | None = None


class PersonPermissionInput(BaseModel):
    person_id: str
    permission: str
    idempotency_key: str | None = None
    confirm: str | None = None


class LegacyPersonGetInput(BaseModel):
    person_id: str
    data_sources: Path


def _people() -> People:
    import secrets
    from t6.cli import DEFAULT_REGISTRY_DB, REGISTRY_DB_VARIABLE, _open_registry
    from t6.person import People
    from t6.preview import PreviewTokens
    registry = _registry.get() or _open_registry(Path(os.environ.get(REGISTRY_DB_VARIABLE, DEFAULT_REGISTRY_DB)))
    if registry is None:
        raise ValueError("Registry is unavailable")
    return People(registry, PreviewTokens(os.environ.get("T6_PREVIEW_SIGNING_KEY") or secrets.token_urlsafe(32)))


def person_write(inputs: PersonAddInput | PersonTelegramInput | PersonRebindInput | PersonShiftInput | PersonClassifyInput | PersonPermissionInput, open_notion: Callable[..., Any]) -> Outcome:
    if not os.environ.get("T6_PREVIEW_SIGNING_KEY"):
        return Refused(rule="config", reason="Set T6_PREVIEW_SIGNING_KEY")
    try:
        values = inputs.model_dump(exclude={"confirm", "idempotency_key"})
        return _people().write("add" if isinstance(inputs, PersonAddInput) else "rebind" if isinstance(inputs, PersonRebindInput) else "shift" if isinstance(inputs, PersonShiftInput) else "classify" if isinstance(inputs, PersonClassifyInput) else "set-telegram", values,
                               actor=_actor.get(),
                               idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)
    except (ValueError, OSError) as error:
        return Refused(rule="input", reason=str(error))


def person_permission(people: People, verb: str, inputs: PersonPermissionInput, *, actor: str) -> Outcome:
    """Run a permission write as the authenticated Person."""
    if not os.environ.get('T6_PREVIEW_SIGNING_KEY'):
        return Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
    return people.write(verb, inputs.model_dump(exclude={'confirm', 'idempotency_key'}),
                        actor=actor, idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)


def person_get(inputs: LegacyPersonGetInput, open_notion: Callable[..., Any]) -> Outcome:
    return Refused(rule="api", reason="Person reads run on the t6 API")


class ApprovalRequestInput(BaseModel):
    gate: str
    target: str
    tapper_rule: str | dict[str, Any] | None = None
    version: str | None = None
    idempotency_key: str | None = None
    confirm: str | None = None


class ApprovalGrantInput(BaseModel):
    approval: str
    idempotency_key: str | None = None
    confirm: str | None = None


class ApprovalDenyInput(ApprovalGrantInput):
    reason: str


class ApprovalOutput(BaseModel):
    approval_id: str
    action: str
    idempotency_key: str
    gate: str | None = None
    target: str | None = None
    tapper_rule: str | dict[str, Any] | None = None
    reason: str | None = None


def approval_write(inputs: ApprovalRequestInput | ApprovalGrantInput | ApprovalDenyInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.approval import Approvals
    from t6.preview import PreviewTokens
    key = os.environ.get('T6_PREVIEW_SIGNING_KEY', '')
    if not key:
        return Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
    verb = 'request' if isinstance(inputs, ApprovalRequestInput) else 'deny' if isinstance(inputs, ApprovalDenyInput) else 'grant'
    return Approvals(_people().registry, PreviewTokens(key)).write(verb, inputs.model_dump(exclude={'confirm', 'idempotency_key'}), actor=_actor.get(), idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)


@dataclass(frozen=True)
class Verb:
    noun: str
    verb: str
    description: str
    aliases: tuple[str, ...]
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    role: str
    rule: str
    handler: Callable[..., Outcome] | None
    local_only: bool = False
    read_only: bool = False


class PersonWriteOutput(BaseModel):
    action: str
    person_id: str
    idempotency_key: str
    page_id: str | None = None
    kind: str | None = None
    departments: list[str] | None = None
    roles: list[str] | None = None
    telegram_id: int | None = None
    master_brief: bool | None = None
    active: bool | None = None
    pattern: str | None = None
    start: str | None = None
    end: str | None = None
    permission: str | None = None
    project: str | None = None
    role: str | None = None


class FormatFinding(BaseModel):
    code: str
    params: dict[str, Any]


class JoFormatOutput(BaseModel):
    text: str
    findings: list[FormatFinding]


class PersonCreateOutput(BaseModel):
    person: dict[str, Any]


class CameraEnrichOutput(BaseModel):
    id: str
    name: str | None
    related_to_projects_cameras: list[str]
    tags: list[str]
    wiki: list[str]


class LegacyJoGetInput(BaseModel):
    ref: str
    data_sources: Path


class JoRaiseMessage(BaseModel):
    """The message a Job Order is raised from, as posted. The API applies every rule to it.

    The API builds the Name, JO Number, Department, Deadline, Telegram Key and
    duplicate key. `assignees` are T6 Person IDs or Contact page IDs; "end of shift"
    reads their End of Shift. `cc` are optional CC T6 Person IDs or Contact page IDs.
    `project` is a T6 Project ID or page ID (empty with `no_project`).
    `department_text` is used only when the header has no Department, and
    `number_text` only when the header does not parse.
    """

    model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)
    header_text: str = ""
    deadline_text: str = ""
    posted_at: AwareDatetime
    chat_id: str | None = None
    message_id: str | None = None
    assignees: list[str] = []
    cc: list[str] = []
    project: str | None = None
    department_text: str | None = None
    number_text: str | None = None


class JoRaiseInput(BaseModel):
    file: Path | None = None
    data_sources: Path | None = None
    fields: dict[str, Any] | None = None
    message: JoRaiseMessage | None = None
    no_project: bool = False
    idempotency_key: str | None = None
    confirm: str | None = None


class JoWriteOutput(BaseModel):
    jo_id: str
    project_id: str | None
    action: str
    idempotency_key: str
    page_id: str | None = None
    sync_intent_id: int | None = None
    name: str | None = None
    jo_number: str | None = None
    department: str | None = None
    deadline: str | None = None
    deadline_source: str | None = None
    deadline_findings: list[dict[str, Any]] | None = None
    department_findings: list[dict[str, Any]] | None = None
    telegram_key: str | None = None
    duplicate_key: str | None = None
    cc_ids: list[str] | None = None


class JoAssignInput(BaseModel):
    jo: str
    person: str
    idempotency_key: str | None = None
    confirm: str | None = None


class JoReassignInput(JoAssignInput):
    reason: str


class JoNoteInput(BaseModel):
    jo: str
    text: str
    idempotency_key: str | None = None
    confirm: str | None = None


class JoSubmitInput(BaseModel):
    jo: str
    idempotency_key: str | None = None
    confirm: str | None = None


class JoCloseInput(JoSubmitInput):
    pass


class JoUnblockInput(JoSubmitInput):
    pass


class JoBlockInput(JoSubmitInput):
    reason: str


class JoCancelInput(JoBlockInput):
    pass


class JoDelayInput(JoBlockInput):
    until: str


def jo_write(inputs: JoRaiseInput | JoAssignInput | JoReassignInput | JoNoteInput | JoSubmitInput, open_notion: Callable[..., Any],
             data_source: str | None = None) -> Outcome:
    """Write a Job Order verb. `data_source` is the server's own Job Orders data source; the CLI, which has none, reads a file.

    A `JoRaiseInput.message` is raw text: the rules (Name, number, Department, deadline, keys) run here, never in the caller."""
    from t6.cli import TOKEN_VARIABLE, _read_data_sources
    from t6.cli_jo import check_fields, prepare_fields
    from t6.job_order import JobOrderStore
    from t6.notion import NotionError
    from t6.preview import PreviewTokens

    key = os.environ.get("T6_PREVIEW_SIGNING_KEY", "")
    if not key:
        return Refused(rule="config", reason="Set T6_PREVIEW_SIGNING_KEY")
    try:
        registry = _people().registry
        values: dict[str, Any]
        derived = None
        state_verbs = {JoSubmitInput: 'submit', JoCloseInput: 'close', JoUnblockInput: 'unblock',
                       JoBlockInput: 'block', JoCancelInput: 'cancel', JoDelayInput: 'delay'}
        if type(inputs) in state_verbs:
            verb = state_verbs[type(inputs)]
            values = inputs.model_dump(exclude={'idempotency_key', 'confirm'})
        elif isinstance(inputs, JoRaiseInput):
            if inputs.message is not None and inputs.file is not None:
                return Refused(rule="input", reason="A message goes with fields, not file")
            if inputs.message is None and (inputs.file is None) == (inputs.fields is None):
                return Refused(rule="input", reason="Give exactly one of file and fields")
            token = os.environ.get(TOKEN_VARIABLE)
            if _notion_source.get() is None and not token:
                return Refused(rule="credentials", reason=f"set {TOKEN_VARIABLE}")
            if data_source is None and inputs.data_sources is not None:
                data_source = _read_data_sources(inputs.data_sources)["job_orders"]
            if data_source is None:
                return Refused(rule="config", reason="The job_orders data source is not configured")
            notion = _verb_notion_source(open_notion, token or "")
            if inputs.message is not None:
                from t6.job_order import apply_message
                built = apply_message(notion, data_source, _people(), inputs.message, inputs.fields or {}, no_project=inputs.no_project,
                                      aliases=os.environ.get("T6_DEPARTMENT_ALIASES", ""))
                if isinstance(built, Refused):
                    return built
                fields, derived = built
            elif inputs.fields is not None:
                fields = check_fields(notion, data_source, inputs.fields)
            else:
                assert inputs.file is not None
                fields = prepare_fields(notion, data_source, inputs.file)
            verb, values = "raise", {"fields": fields, **({"no_project": True} if inputs.no_project else {})}
        elif isinstance(inputs, JoReassignInput):
            verb, values = "reassign", {"jo": inputs.jo, "person": inputs.person, "reason": inputs.reason}
        elif isinstance(inputs, JoAssignInput):
            verb, values = "assign", {"jo": inputs.jo, "person": inputs.person}
        elif isinstance(inputs, JoNoteInput):
            verb, values = "note", {"jo": inputs.jo, "text": inputs.text}
        else:
            raise ValueError('Unknown Job Order input')
        return JobOrderStore(registry, PreviewTokens(key)).write(verb, values, actor=_actor.get(),
            idempotency_key=inputs.idempotency_key, confirm=inputs.confirm, derived=derived)
    except (ValueError, OSError, KeyError, TypeError, NotionError) as error:
        return Refused(rule="input", reason=str(error))


class ConcernRaiseInput(BaseModel):
    project: str
    owner: str | None = None
    waiting_on: str | None = None
    anchor: str
    as_of: str
    text: str | None = None
    source: Literal['person', 'pm_notes_scan', 'date_rule', 'flag_rule', 'pm_notes_read', 'leave'] = 'person'
    settles_id: str | None = None
    settles_deliverable: str | None = None
    settles_milestone: str | None = None
    settles_date: Literal['playdate', 'submission_deadline', 'archival_deadline'] | None = None
    idempotency_key: str | None = None
    confirm: str | None = None


class ConcernNoteInput(BaseModel):
    concern: str
    text: str
    from_reply: str | None = None
    message_id: str | None = None
    idempotency_key: str | None = None
    confirm: str | None = None


class ConcernRecordMessageInput(BaseModel):
    concern: str
    chat: str
    message_id: str
    brief_date: str | None = None
    idempotency_key: str | None = None
    confirm: str | None = None


class ConcernForMessageInput(BaseModel):
    chat: str
    message_id: str


class ConcernForMessageOutput(BaseModel):
    concern_ids: list[str]


class ConcernResolveInput(BaseModel):
    concern: str
    settles_id: str | None = None
    idempotency_key: str | None = None
    confirm: str | None = None


class ConcernReopenInput(BaseModel):
    concern: str
    idempotency_key: str | None = None
    confirm: str | None = None


class ConcernScanInput(BaseModel):
    project: str
    owner: str


class ConcernChangesInput(BaseModel):
    project: str
    since: str


class ConcernChangesOutput(BaseModel):
    changes: list[dict[str, Any]]


class ConcernScanOutput(BaseModel):
    project: str


class ConcernListInput(BaseModel):
    raise_at_psr: bool = False
    open: bool = False
    project: str | None = None


class ConcernListOutput(BaseModel):
    concerns: list[dict[str, Any]]
    pending: list[dict[str, Any]] = []


class ConcernFinding(BaseModel):
    project: str
    text: str
    source: Literal['flag_rule', 'pm_notes_read', 'leave']
    owner: str | None = None
    waiting_on: str | None = None


class ConcernUnavailable(BaseModel):
    project: str
    source: Literal['flag_rule', 'pm_notes_read', 'leave']


class ConcernObserveInput(BaseModel):
    run_date: str
    findings: list[ConcernFinding]
    unavailable: list[ConcernUnavailable] = []
    dry_run: bool = False
    idempotency_key: str


class ConcernObserveOutput(BaseModel):
    run_date: str
    projects: list[dict[str, Any]]
    counts: dict[str, int]


class ConcernCountsInput(BaseModel):
    since: str | None = None
    until: str | None = None
    project: str | None = None


class ConcernCountsOutput(BaseModel):
    runs: list[dict[str, Any]]
    closed_in_range: int
    median_days_open: float | None
    still_open: int


class ConcernGetInput(BaseModel):
    concern: str


class ConcernOutput(BaseModel):
    concern_id: str
    project_id: str
    action: str
    idempotency_key: str


class ConcernGetOutput(BaseModel):
    concern: dict[str, Any]


def concern_write(inputs: ConcernRaiseInput | ConcernNoteInput | ConcernResolveInput | ConcernReopenInput | ConcernRecordMessageInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.concern import ConcernStore
    from t6.preview import PreviewTokens
    key = os.environ.get('T6_PREVIEW_SIGNING_KEY', '')
    if not key:
        return Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
    verb = {ConcernRaiseInput: 'raise', ConcernNoteInput: 'note', ConcernResolveInput: 'resolve', ConcernReopenInput: 'reopen', ConcernRecordMessageInput: 'record-message'}[type(inputs)]
    values = inputs.model_dump(exclude={'idempotency_key', 'confirm'})
    return ConcernStore(_people().registry, PreviewTokens(key)).write(verb, values, actor=_actor.get(), idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)


def concern_for_message(inputs: ConcernForMessageInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.concern import ConcernStore
    return Done(result={'concern_ids': ConcernStore(_people().registry, _people().tokens).for_message(inputs.chat, inputs.message_id)})


def concern_changes(inputs: ConcernChangesInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.concern import ConcernStore
    from t6.concern_scan import ConcernScanner
    try:
        return Done(result={'changes': ConcernScanner(ConcernStore(_people().registry, _people().tokens)).changes(inputs.project, inputs.since)})
    except ValueError:
        return Refused(rule='since', reason='Since must be an ISO date')


def concern_list(inputs: ConcernListInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.concern import ConcernStore
    if inputs.raise_at_psr == inputs.open:
        return Refused(rule='input', reason='Pass exactly one of raise_at_psr and open')
    store = ConcernStore(_people().registry, _people().tokens)
    return Done(result={'concerns': store.open_concerns(inputs.project) if inputs.open else store.raise_at_psr(inputs.project), 'pending': store.pending_closes(inputs.project)})


def concern_scan(inputs: ConcernScanInput, open_notion: Callable[..., Any]) -> Outcome:
    return Refused(rule='api', reason='Concern scanning runs on the t6 API')


def concern_observe(inputs: ConcernObserveInput, open_notion: Callable[..., Any]) -> Outcome:
    return Refused(rule='api', reason='Concern observe runs on the t6 API')


def concern_counts(inputs: ConcernCountsInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.concern import ConcernStore
    return ConcernStore(_people().registry, _people().tokens).counts(inputs.since, inputs.until, inputs.project)


def concern_get(inputs: ConcernGetInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.concern import ConcernStore
    record = ConcernStore(_people().registry, _people().tokens).get(inputs.concern)
    return Done(result={'concern': record}) if record else Refused(rule='concern', reason='Concern not found')


class DeliverableDeliverInput(BaseModel):
    deliverable: str
    carrier: str
    ref: str | None = None
    idempotency_key: str | None = None
    confirm: str | None = None


class DeliverableDeliverOutput(BaseModel):
    package_id: str
    milestone_id: str
    deliverable_id: str
    project_id: str
    carrier: str
    ref: str | None = None
    idempotency_key: str


def deliverable_deliver(inputs: DeliverableDeliverInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.package import PackageStore
    return PackageStore(_people().registry, _people().tokens).deliver(
        inputs.model_dump(exclude={'idempotency_key', 'confirm'}), actor=_actor.get(),
        idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)


class PackagePrepareInput(BaseModel):
    deliverable: str
    kind: str
    to: str
    recipient: str
    data_sources: Path
    client_spec: str | None = None
    note: str | None = None
    idempotency_key: str | None = None
    confirm: str | None = None


class PackageCheckInput(BaseModel):
    package: str
    item: str
    idempotency_key: str | None = None
    confirm: str | None = None


class PackageHandoffInput(BaseModel):
    package: str
    idempotency_key: str | None = None
    confirm: str | None = None


class PackageAcceptInput(PackageHandoffInput):
    pass


class PackageRejectInput(PackageHandoffInput):
    reason: str = ''


class PackageListInput(BaseModel):
    missing: bool = False


class PackageGetInput(BaseModel):
    package: str


class PackageOutput(BaseModel):
    package_id: str
    project_id: str
    action: str
    idempotency_key: str


class PackageListOutput(BaseModel):
    packages: list[dict[str, Any]]


class PackageGetOutput(BaseModel):
    package: dict[str, Any]


def package_write(inputs: PackagePrepareInput | PackageCheckInput | PackageHandoffInput | PackageAcceptInput | PackageRejectInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.package import PackageStore
    from t6.preview import PreviewTokens
    from t6.cli import TOKEN_VARIABLE, _read_data_sources
    from t6.notion import NotionError
    key = os.environ.get('T6_PREVIEW_SIGNING_KEY', '')
    if not key:
        return Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
    verb = {PackagePrepareInput: 'prepare', PackageCheckInput: 'check', PackageHandoffInput: 'handoff', PackageAcceptInput: 'accept', PackageRejectInput: 'reject'}[type(inputs)]
    values = inputs.model_dump(exclude={'idempotency_key', 'confirm', 'data_sources'})
    store = PackageStore(_people().registry, PreviewTokens(key))
    try:
        if isinstance(inputs, PackagePrepareInput):
            from t6.notion.package_specs import PackageSpecReader
            token = os.environ.get(TOKEN_VARIABLE)
            if _notion_source.get() is None and not token:
                return Refused(rule='credentials', reason=f'set {TOKEN_VARIABLE}')
            notion = _verb_notion_source(open_notion, token or '')
            store.specs = PackageSpecReader(notion, _read_data_sources(inputs.data_sources)['package_specs'])
            return store.write(verb, values, actor=_actor.get(), idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)
        return store.write(verb, values, actor=_actor.get(), idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)
    except (OSError, ValueError, KeyError, NotionError) as error:
        return Refused(rule='spec', reason=str(error))


def package_list(inputs: PackageListInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.package import PackageStore
    return Done(result={'packages': PackageStore(_people().registry, _people().tokens).list(inputs.missing)})


class QueueFailedInput(BaseModel):
    pass


class QueueFailedOutput(BaseModel):
    jobs: list[dict[str, Any]]


class QueueDismissInput(BaseModel):
    job: int
    reason: str
    idempotency_key: str | None = None
    confirm: str | None = None


class QueueDismissOutput(BaseModel):
    job: int
    queue: str
    kind: str
    dedupe_key: str
    failure: Any
    reason: str
    state: str
    idempotency_key: str


def queue_failed(inputs: QueueFailedInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.queue_dismiss import JobDismissals
    return Done(result={'jobs': JobDismissals(_people().registry, _people().tokens).failed()})


def queue_dismiss(inputs: QueueDismissInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.preview import PreviewTokens
    from t6.queue_dismiss import JobDismissals
    key = os.environ.get('T6_PREVIEW_SIGNING_KEY', '')
    if not key:
        return Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
    return JobDismissals(_people().registry, PreviewTokens(key)).write(
        {'job': inputs.job, 'reason': inputs.reason}, actor=_actor.get(), idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)


def package_get(inputs: PackageGetInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.package import PackageStore
    record = PackageStore(_people().registry, _people().tokens).get(inputs.package)
    return Done(result={'package': record}) if record else Refused(rule='package', reason='Package not found')


class JoFormatInput(BaseModel):
    file: Path
    date: str | None = None


class LegacyPersonFindInput(BaseModel):
    mention: str | None = None
    mentions: list[str] | None = None
    suggest: bool = False
    prefer_telegram: bool = False
    data_sources: Path


class LegacyProjectResolveInput(BaseModel):
    text: str
    data_sources: Path
    registry_db: Path | None = None
    in_text: bool = False


class LegacyCameraGetInput(BaseModel):
    data_sources: Path
    page_id: str | None = None


class CameraEnrichInput(BaseModel):
    page_id: str
    fields: dict[str, Any]
    idempotency_key: str | None = None
    confirm: str | None = None


def llm_api_only(inputs: LLMRunInput, open_notion: Callable[..., Any]) -> Outcome:
    return Refused(rule="api", reason="LLM tasks run on the t6 API")


def llm_decide_api_only(inputs: LLMDecideInput, open_notion: Callable[..., Any]) -> Outcome:
    return Refused(rule="api", reason="LLM tasks run on the t6 API")


def llm_filter_api_only(inputs: LLMFilterInput, open_notion: Callable[..., Any]) -> Outcome:
    return Refused(rule="api", reason="LLM tasks run on the t6 API")


class ReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DepartmentsInput(ReadInput):
    include_misc: bool = False


class PersonGetInput(ReadInput):
    person_id: str


class PersonFindInput(ReadInput):
    mention: str | None = None
    mentions: list[str] | None = None
    suggest: bool = False
    prefer_telegram: bool = False


class PersonListInput(ReadInput):
    registered: bool | None = None
    kind: PersonKind | None = None


class ProjectGetInput(ReadInput):
    project: str


class ProjectNotesInput(ReadInput):
    project: str


class ProjectNotesOutput(BaseModel):
    lines: list[dict[str, str]]


class ProjectNotesHistoryInput(ReadInput):
    project: str


class ProjectNotesHistoryOutput(BaseModel):
    lines: list[dict[str, str | None]]


class ProjectFlagsItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str
    stated: dict[Literal["playdate", "submission_deadline", "archival_deadline"], IsoDate | None] = {}


class ProjectFlagsInput(ReadInput):
    project: str | None = None
    projects: list[ProjectFlagsItem] = Field(default=[], max_length=500)
    """Several Projects in one call, each with its own `stated`, in place of `project`; the Projects are read once."""
    as_of: IsoDate | None = None
    """The day the flags are judged on; the Manila day today when not given."""
    lead_warning_days: int = 30
    stated: dict[Literal["playdate", "submission_deadline", "archival_deadline"], IsoDate | None] = {}
    """A date the caller read from the PM Notes for a blank date property. `None` is a fuzzy value: not missing, never overdue."""

    @model_validator(mode="after")
    def _one_or_several(self) -> "ProjectFlagsInput":
        if (self.project is None) == (not self.projects) or (self.projects and self.stated):
            raise ValueError("Give project (with stated) or projects (each with its own stated)")
        return self


class DateSanityProblem(BaseModel):
    kind: Literal["submission_before_playdate", "archival_before_submission"]
    first: Literal["playdate", "submission_deadline", "archival_deadline"]
    second: Literal["playdate", "submission_deadline", "archival_deadline"]
    first_date: IsoDate
    second_date: IsoDate
    from_notes: bool


class ProjectFlagsOutput(BaseModel):
    flags: list[dict[str, Any]] = []
    missing_roles: list[str] = []
    sanity: list[DateSanityProblem] = []
    projects: list[dict[str, Any]] = []
    """For `projects` in the input, one entry each in the same order: `project` as sent, `found`, and when found its `flags`, `missing_roles` and `sanity`."""


class ProjectResolveInput(ReadInput):
    text: str
    in_text: bool = False


class JoGetInput(ReadInput):
    ref: str


class CameraGetInput(ReadInput):
    page_id: str | None = None


class TextWithholdInput(BaseModel):
    texts: list[str] = Field(max_length=200)


class TextWithholdOutput(BaseModel):
    texts: list[str]
    withheld: int


def text_withhold(inputs: TextWithholdInput, open_notion: Callable[..., Any]) -> Outcome:
    """Message text an app is about to show, with labelled credential values replaced; the rule is `t6.privacy.withhold`."""
    from t6.privacy.withhold import withhold
    done = [withhold(text) for text in inputs.texts]
    return Done(result={'texts': [w.text for w in done], 'withheld': sum(w.count for w in done)})


class StandingCcInput(ReadInput):
    pass


class StudioNoticesInput(ReadInput):
    pass


class NoticeRun(BaseModel):
    text: str
    bold: bool = False


class StudioNoticesOutput(BaseModel):
    description: str
    runs: list[NoticeRun] = []


class ExternalLinkFindInput(ReadInput):
    system: str
    kind: str
    external_id: str


class ExternalLinkRead(BaseModel):
    page_id: str
    ref: ExternalRef
    active: bool
    last_activity: datetime | None
    project_ids: list[str]


class ExternalLinkFindOutput(BaseModel):
    link: ExternalLinkRead | None


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class ReadProject(Project):
    """A Project as the API reads it: with the results of the Studio rules (`t6.domain.project_rules`).

    Apps do not evaluate these rules. `missing_roles` are Project attribute names in report order;
    `relevant_dates` are Project date attribute names.
    """

    status_key: str = ""
    status_group: Literal["in_progress", "on_hold", "incoming"] | None = None
    snapshotted: bool = True
    missing_roles: tuple[str, ...] = ()
    relevant_dates: tuple[str, ...] = ()
    health_missing: bool = False
    health_rank: int = 0


class ProjectListInput(ReadInput):
    rotation: Literal["in", "out", "unclassified"] | None = None
    status: str | None = None
    status_group: Literal["in_progress", "on_hold", "incoming"] | None = None


class ProjectListOutput(BaseModel):
    projects: list[ReadProject]


class JoListInput(ReadInput):
    project: str | None = None
    assignee: str | None = None
    status_group: Literal["not_started", "started", "cancelled", "active"] | None = None
    no_project: bool = False


class JoFindInput(ReadInput):
    """Give exactly one form: `telegram_key`, `chat_id` with `message_id`, or `project` with `number` (the duplicate key)."""

    model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)
    telegram_key: str | None = None
    chat_id: str | None = None
    message_id: str | None = None
    project: str | None = None
    number: str | None = None


class DeliverableDefineInput(BaseModel):
    project: str
    code: str
    template: str | None = None
    episode: str | None = None
    due: str | None = None
    air: str | None = None
    idempotency_key: str | None = None
    confirm: str | None = None


class DeliverableEditInput(BaseModel):
    deliverable: str
    code: str | None = None
    episode: str | None = None
    due: str | None = None
    air: str | None = None
    idempotency_key: str | None = None
    confirm: str | None = None


class DeliverableRebindInput(BaseModel):
    deliverable_id: str
    page_id: str
    idempotency_key: str | None = None
    confirm: str | None = None


def deliverable_rebind(inputs: DeliverableRebindInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.deliverable import DeliverableStore
    if not os.environ.get('T6_PREVIEW_SIGNING_KEY'):
        return Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
    people = _people()
    return DeliverableStore(people.registry, people.tokens).rebind(
        inputs.model_dump(exclude={'idempotency_key', 'confirm'}), actor=_actor.get(),
        idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)


class DeliverableEditOutput(BaseModel):
    action: str
    deliverable_id: str
    project_id: str
    before: dict[str, Any]
    after: dict[str, Any]
    idempotency_key: str


def deliverable_edit(inputs: DeliverableEditInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.deliverable import DeliverableStore
    if not os.environ.get('T6_PREVIEW_SIGNING_KEY'):
        return Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
    people = _people()
    return DeliverableStore(people.registry, people.tokens).edit(
        inputs.model_dump(exclude={'idempotency_key', 'confirm'}), actor=_actor.get(),
        idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)


class DeliverableMigrateInput(BaseModel):
    data_sources: Path
    registry_db: Path | None = None
    apply: bool = False
    preview: Path | None = None


class DeliverableMigrateOutput(BaseModel):
    digest: str
    entries: list[dict[str, Any]]
    review: list[dict[str, Any]]


class VersionAddInput(BaseModel):
    deliverable: str
    frameio_file: str
    note: str | None = None
    timeline: str | None = None
    idempotency_key: str | None = None
    confirm: str | None = None


class VersionGetInput(BaseModel):
    version: str


class VersionFindInput(BaseModel):
    frameio_file: str


class VersionFindOutput(BaseModel):
    version: dict[str, Any] | None


class VersionLockInput(BaseModel):
    version: str
    idempotency_key: str | None = None
    confirm: str | None = None


class VersionOutput(BaseModel):
    version_id: str
    deliverable_id: str
    project_id: str
    idempotency_key: str


def version_write(inputs: VersionAddInput | VersionLockInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.version import VersionStore
    from t6.frameio.client import FrameioReader
    from t6.worker.client import HttpTransport
    if not os.environ.get('T6_PREVIEW_SIGNING_KEY'):
        return Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
    url, secret = os.environ.get('T6_FRAMEIO_READ_URL'), os.environ.get('T6_FRAMEIO_READ_SECRET')
    reader = FrameioReader(HttpTransport(url), secret) if url and secret else None
    return VersionStore(_people().registry, _people().tokens, reader).write(
        'add' if isinstance(inputs, VersionAddInput) else 'lock',
        inputs.model_dump(exclude={'confirm', 'idempotency_key'} | ({'timeline'} if getattr(inputs, 'timeline', None) is None else set())), actor=_actor.get(),
        idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)


class MilestoneRecordInput(BaseModel):
    deliverable: str
    milestone: str
    on: str | None = None
    idempotency_key: str | None = None
    confirm: str | None = None


class MilestoneRecordOutput(BaseModel):
    milestone_id: str
    deliverable_id: str
    project_id: str
    milestone: str
    on: str
    idempotency_key: str


def milestone_record(inputs: MilestoneRecordInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.deliverable import DeliverableStore
    if not os.environ.get('T6_PREVIEW_SIGNING_KEY'):
        return Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
    people = _people()
    return DeliverableStore(people.registry, people.tokens).record_milestone(
        inputs.model_dump(exclude={'idempotency_key', 'confirm'}), actor=_actor.get(),
        idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)


class DeliverableMissInput(BaseModel):
    deliverable: str
    reason: str
    idempotency_key: str | None = None
    confirm: str | None = None


class DeliverableDelayInput(DeliverableMissInput):
    field: Literal['due', 'air']
    date: str


class DeliverableScheduleOutput(BaseModel):
    action: str
    deliverable_id: str
    project_id: str
    reason: str
    idempotency_key: str


def deliverable_schedule(inputs: DeliverableMissInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.deliverable import DeliverableStore
    if not os.environ.get('T6_PREVIEW_SIGNING_KEY'):
        return Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
    people = _people()
    action = 'delay' if isinstance(inputs, DeliverableDelayInput) else 'miss'
    return DeliverableStore(people.registry, people.tokens).schedule_write(
        action, inputs.model_dump(exclude={'idempotency_key', 'confirm'}), actor=_actor.get(),
        idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)


class DeliverableGetInput(BaseModel):
    ref: str


class DeliverableRecord(BaseModel):
    id: str
    t6_id: str
    project_id: str
    code: str
    episode: str | None
    due: str | None
    air: str | None
    bindings: list[dict[str, Any]]
    milestones: list[dict[str, Any]]
    stage: dict[str, Any]


class DeliverableGetOutput(BaseModel):
    deliverable: DeliverableRecord


class DeliverableDefineOutput(BaseModel):
    deliverable_id: str
    project_id: str
    action: str
    idempotency_key: str
    page_id: str | None = None


def deliverable_define(inputs: DeliverableDefineInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.deliverable import DeliverableStore
    if not os.environ.get('T6_PREVIEW_SIGNING_KEY'):
        return Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
    from t6.cli import TOKEN_VARIABLE
    from t6.notion.generated import ProjectsRow
    from t6.notion import NotionError

    def project_type(page: str) -> str | None:
        from uuid import UUID

        token = os.environ.get(TOKEN_VARIABLE)
        if _notion_source.get() is None and not token:
            raise ValueError(f'set {TOKEN_VARIABLE} to read Project Type')
        if len(page) == 32:
            try:
                page = str(UUID(page))
            except ValueError:
                pass
        notion = _verb_notion_source(open_notion, token or '')
        return ProjectsRow.from_page(notion.retrieve_page(page, freshness='live')).project_type

    try:
        return DeliverableStore(_people().registry, _people().tokens, project_type).define(
            inputs.model_dump(exclude={'idempotency_key', 'confirm'}), actor=_actor.get(),
            idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)
    except (NotionError, OSError, ValueError) as error:
        return Refused(rule='project_type', reason=str(error))


class DeliverableListInput(ReadInput):
    project: str


class DeliverableCandidatesInput(ReadInput):
    project: str


class DeliverableCandidate(BaseModel):
    page_id: str
    name: str | None = None
    status: str | None = None
    deliverable_class: str | None = None


class DeliverableCandidatesOutput(BaseModel):
    candidates: list[DeliverableCandidate]


class FloorReadInput(ReadInput):
    project: str | None = None


class FloorReadOutput(BaseModel):
    margin_days: int | None = None
    deliverables: list[dict[str, Any]]


def _floor(read: str, inputs: FloorReadInput) -> Outcome:
    from t6.floor import FloorMargins, FloorReads
    try:
        margins = FloorMargins.from_environ(os.environ)
        reads = FloorReads(_people().registry, margins)
        if read == 'sabit':
            return Done(result={'deliverables': reads.sabit(inputs.project)})
        if read == 'bangko':
            return Done(result={'margin_days': margins.bangko_days, 'deliverables': reads.bangko(inputs.project)})
        return Done(result={'margin_days': margins.hand_to_mouth_days, 'deliverables': reads.hand_to_mouth(inputs.project)})
    except ValueError as error:
        return Refused(rule='config', reason=str(error))
    except LookupError as error:
        return Refused(rule='project', reason=str(error))


def floor_bangko(inputs: FloorReadInput, open_notion: Callable[..., Any]) -> Outcome:
    return _floor('bangko', inputs)


def floor_hand_to_mouth(inputs: FloorReadInput, open_notion: Callable[..., Any]) -> Outcome:
    return _floor('hand-to-mouth', inputs)


def floor_sabit(inputs: FloorReadInput, open_notion: Callable[..., Any]) -> Outcome:
    return _floor('sabit', inputs)


class MeetingNotesListInput(ReadInput):
    project: str | None = None
    since: str | None = None
    before: str | None = None
    title: str | None = None
    title_prefix: str | None = None
    ready_to_send: bool | None = None
    latest: bool = False
    include_body: bool = True


@dataclass(frozen=True, slots=True, kw_only=True)
class ReadJobOrder(JobOrder):
    """A Job Order as the API reads it.

    `health` is Notion's Health formula: `None` when it gave no value and `""`
    when it computed an empty string.
    """

    t6_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ReadContact(Contact):
    t6_id: str | None = None
    kind: str | None = None
    departments: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    shift: dict[str, str] | None = None
    project_roles: tuple[dict[str, str], ...] = ()
    permissions: tuple[str, ...] = ()
    master_brief: bool = False
    active: bool = True


@dataclass(frozen=True, slots=True, kw_only=True)
class ReadDeliverable(Deliverable):
    t6_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectCandidate(Project):
    score: float = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class ContactCandidate(ReadContact):
    score: float = 0


class ProjectGetOutput(BaseModel):
    project: ReadProject


class ProjectResolveOutput(BaseModel):
    match: Project | None
    method: str
    score: float
    needs_review: bool
    reason: str | None
    candidates: list[ProjectCandidate]


class PersonGetOutput(BaseModel):
    person: ReadContact


class PersonListOutput(BaseModel):
    people: list[ReadContact]


class PersonFindOutput(BaseModel):
    match: ReadContact | None
    method: str
    score: float
    needs_review: bool
    reason: str | None
    candidates: list[ContactCandidate]


class PersonFindManyOutput(BaseModel):
    results: list[PersonFindOutput]


class JoGetOutput(BaseModel):
    job_order: ReadJobOrder


class CameraGetOutput(BaseModel):
    cameras: list[Camera]


class StandingCcOutput(BaseModel):
    standing_cc: str | None


class JoListOutput(BaseModel):
    job_orders: list[ReadJobOrder]


class DeliverableListOutput(BaseModel):
    deliverables: list[DeliverableRecord]


class NoteSpan(BaseModel):
    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strikethrough: bool = False
    code: bool = False
    url: str | None = None


class NoteBlock(BaseModel):
    kind: str
    text: str
    spans: list[NoteSpan]
    checked: bool | None = None
    children: list[NoteBlock]


@dataclass(frozen=True, slots=True, kw_only=True)
class ReadMeetingNote(MeetingNote):
    body: tuple[NoteBlock, ...] = ()


class MeetingNotesListOutput(BaseModel):
    meeting_notes: list[ReadMeetingNote]


class MeetingNotesCreateInput(BaseModel):
    title: str
    meeting_date: str
    project_ids: list[str] = []
    attendee_ids: list[str] = []
    blocks: list[NoteBlock] = []


class MeetingNotesAppendInput(BaseModel):
    note: str
    blocks: list[NoteBlock]
    idempotency_key: str | None = None


class MeetingNotesTrashInput(BaseModel):
    note: str


class MeetingNotesMarkSentInput(BaseModel):
    note: str
    at: AwareDatetime | None = None


class MeetingNotesWriteOutput(BaseModel):
    meeting_note: MeetingNote


class MachineListInput(ReadInput):
    pass


class MachineListOutput(BaseModel):
    machines: list[Machine]


class MachineFactsInput(BaseModel):
    """What is known about one Machine. A fact left out or blank is unknown and is never written."""

    names: list[str]
    serial_number: str | None = None
    munki_manifest: str | None = None
    ring: Ring | None = None
    munki_client_version: str | None = None
    last_check_in: AwareDatetime | None = None
    os_version: str | None = None
    observed_at: AwareDatetime | None = None

    @field_validator("names")
    @classmethod
    def _named(cls, names: list[str]) -> list[str]:
        if not any(name.strip() for name in names):
            raise ValueError("a machine needs at least one name")
        return names


class MachineSyncInput(BaseModel):
    machines: list[MachineFactsInput]
    confirm: str | None = None


class MachineFactChange(BaseModel):
    fact: str
    old: Any
    new: Any


class MachineFactSkipped(BaseModel):
    fact: str
    reason: Literal["unknown", "stale"]


class MachineSyncResult(BaseModel):
    name: str
    status: Literal["matched", "unmatched", "ambiguous", "changed"]
    page_id: str | None = None
    changes: list[MachineFactChange] = []
    skipped: list[MachineFactSkipped] = []
    written: Literal["saved", "rejected", "ambiguous"] | None = None
    detail: str | None = None


class MachineSyncOutput(BaseModel):
    machines: list[MachineSyncResult]


class MachineEditItem(BaseModel):
    """Edits to one Machine's Bays row. `names` find the row as in `MachineFactsInput`. `values` holds
    properties from the editable list; a property not on it, or a value of the wrong type, fails the call.
    A left-out or blank value means no change. Nothing is cleared."""

    names: list[str]
    values: dict[str, Any]

    @field_validator("names")
    @classmethod
    def _named(cls, names: list[str]) -> list[str]:
        if not any(name.strip() for name in names):
            raise ValueError("a machine needs at least one name")
        return names


class MachineEditInput(BaseModel):
    machines: list[MachineEditItem]
    confirm: str | None = None


class MachineEditResult(BaseModel):
    name: str
    status: Literal["matched", "unmatched", "ambiguous", "changed"]
    page_id: str | None = None
    changes: list[MachineFactChange] = []
    written: Literal["saved", "rejected", "ambiguous"] | None = None
    detail: str | None = None


class MachineEditOutput(BaseModel):
    machines: list[MachineEditResult]


def api_read(inputs: BaseModel, open_notion: Callable[..., Any]) -> Outcome:
    return Refused(rule="api", reason="Reads run on the t6 API")


def api_write(inputs: BaseModel, open_notion: Callable[..., Any]) -> Outcome:
    return Refused(rule="api", reason="This write runs on the t6 API")


class AbangRequestInput(BaseModel):
    person: str
    destination: str
    source: str | None = None
    note: str | None = None
    idempotency_key: str | None = None
    confirm: str | None = None


class BayBookInput(BaseModel):
    bay: str
    person: str
    project: str
    start: str
    end: str | None = None
    shift: Literal['day', 'night'] | None = None
    force: bool = False
    idempotency_key: str | None = None
    confirm: str | None = None


class AbangCancelInput(BaseModel):
    request: str
    idempotency_key: str | None = None
    confirm: str | None = None


class BookingMoveInput(BaseModel):
    booking: str
    start: str
    end: str
    bay: str | None = None
    force: bool = False
    idempotency_key: str | None = None
    confirm: str | None = None


class AbangAcceptInput(BaseModel):
    candidate: str
    decision: str
    idempotency_key: str | None = None
    confirm: str | None = None


class BookingExtendInput(BaseModel):
    booking: str
    end: str
    force: bool = False
    idempotency_key: str | None = None
    confirm: str | None = None


class BookingCancelInput(BaseModel):
    booking: str
    idempotency_key: str | None = None
    confirm: str | None = None


class BayListInput(BaseModel):
    pass


class BayListOutput(BaseModel):
    bays: list[dict[str, Any]]


def bay_list(inputs: BayListInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.bay import BayStore
    return Done(result={'bays': BayStore(_people().registry).list()})


class BayScanReportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report: BayScanReport


class BayScanReportOutput(BaseModel):
    bay_id: str
    stored: bool
    reason: str


class BayScanListInput(ReadInput):
    bay: str | None = None


class BayScanListOutput(BaseModel):
    bays: list[dict[str, Any]]


class BayDriftInput(ReadInput):
    project: str | None = None


class BayDriftOutput(BaseModel):
    matched_by: str
    note: str
    bays: list[dict[str, Any]]
    projects: list[dict[str, Any]]


def bay_scan_list(inputs: BayScanListInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.bay_scan import BayScans
    return BayScans(_people().registry).list(inputs.bay)


class BookingGetInput(BaseModel):
    booking: str


class BookingListInput(BaseModel):
    pass


class BookingOutput(BaseModel):
    booking_id: str
    bay_id: str
    person_id: str
    project_id: str
    start: str
    end: str
    forced: bool
    cancelled: bool
    idempotency_key: str


class BookingGetOutput(BaseModel):
    booking: dict[str, Any]


class BookingListOutput(BaseModel):
    bookings: list[dict[str, Any]]


def booking_write(inputs: BayBookInput | BookingMoveInput | BookingExtendInput | BookingCancelInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.bay import BookingStore
    if not os.environ.get('T6_PREVIEW_SIGNING_KEY'):
        return Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
    people = _people()
    verb = 'book' if isinstance(inputs, BayBookInput) else 'move' if isinstance(inputs, BookingMoveInput) else 'extend' if isinstance(inputs, BookingExtendInput) else 'cancel'
    return BookingStore(people.registry, people.tokens).write(verb, inputs.model_dump(exclude={'idempotency_key', 'confirm'}), actor=_actor.get(), idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)


def booking_read(inputs: BookingGetInput | BookingListInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.bay import BookingStore
    people = _people()
    store = BookingStore(people.registry, people.tokens)
    if isinstance(inputs, BookingListInput):
        return Done(result={'bookings': store.list()})
    booking = store.get(inputs.booking)
    return Done(result={'booking': booking}) if booking else Refused(rule='booking', reason='Booking not found')


class MediaMigrateInput(BaseModel):
    idempotency_key: str | None = None
    confirm: str | None = None


class MediaMigrateOutput(BaseModel):
    digest: str
    entries: list[dict[str, Any]]
    review: list[dict[str, Any]]
    idempotency_key: str


class MediaLinkInput(BaseModel):
    owner: str
    kind: MediaKind
    ref: str
    role: MediaRole
    idempotency_key: str | None = None
    confirm: str | None = None


class MediaUnlinkInput(BaseModel):
    media: str
    idempotency_key: str | None = None
    confirm: str | None = None


class FrameioProjectInput(BaseModel):
    project: str
    frameio_project: str
    idempotency_key: str | None = None
    confirm: str | None = None


class MediaListInput(BaseModel):
    owner: str


class MediaWriteOutput(BaseModel):
    media_id: str
    project_id: str
    idempotency_key: str


class MediaListOutput(BaseModel):
    locations: list[dict[str, Any]]


def media_write(inputs: MediaLinkInput | MediaUnlinkInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.media import MediaStore
    if not os.environ.get('T6_PREVIEW_SIGNING_KEY'):
        return Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
    people = _people()
    person = people.get(_actor.get())
    if not person or not person['active']:
        return Refused(rule='person', reason='An active Person is required')
    return MediaStore(people.registry, people.tokens).write(
        'link' if isinstance(inputs, MediaLinkInput) else 'unlink',
        inputs.model_dump(exclude={'confirm', 'idempotency_key'}), actor=_actor.get(),
        idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)


def _frameio_project(verb: str, inputs: FrameioProjectInput) -> Outcome:
    from t6.media import MediaStore
    if not os.environ.get('T6_PREVIEW_SIGNING_KEY'):
        return Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
    people = _people()
    return MediaStore(people.registry, people.tokens).frameio_project_write(
        verb, inputs.model_dump(exclude={'confirm', 'idempotency_key'}), actor=_actor.get(),
        idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)


def frameio_project_link(inputs: FrameioProjectInput, open_notion: Callable[..., Any]) -> Outcome:
    return _frameio_project('link', inputs)


def frameio_project_unlink(inputs: FrameioProjectInput, open_notion: Callable[..., Any]) -> Outcome:
    return _frameio_project('unlink', inputs)


def media_list(inputs: MediaListInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.media import MediaStore
    people = _people()
    return Done(result={'locations': MediaStore(people.registry, people.tokens).list(inputs.owner)})


class TimelineLinkInput(BaseModel):
    project: str
    timeline: str
    stamp: str | None = None
    idempotency_key: str | None = None
    confirm: str | None = None


StampState = Literal['pending', 'stamped', 'failed']


class TimelineWriteOutput(BaseModel):
    timeline_id: str
    project_id: str
    created: bool
    idempotency_key: str
    stamp_state: StampState
    stamp_job: int | None = None


class TimelineRestampInput(BaseModel):
    project: str
    timeline: str
    stamp: str
    idempotency_key: str | None = None
    confirm: str | None = None


class TimelineRestampOutput(TimelineWriteOutput):
    replaces: str


class TimelineListInput(BaseModel):
    project: str


class TimelineEntry(BaseModel):
    id: str
    stamp: str
    project_id: str
    stamp_state: StampState
    stamp_reason: str | None = None
    canonical: dict[str, Any]


class TimelineListOutput(BaseModel):
    timelines: list[TimelineEntry]


def timeline_write(inputs: TimelineLinkInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.timeline import TimelineStore
    if not os.environ.get('T6_PREVIEW_SIGNING_KEY'):
        return Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
    people = _people()
    return TimelineStore(people.registry, people.tokens).write(
        inputs.model_dump(exclude={'confirm', 'idempotency_key'}), actor=_actor.get(),
        idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)


def timeline_restamp(inputs: TimelineRestampInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.timeline import TimelineStore
    if not os.environ.get('T6_PREVIEW_SIGNING_KEY'):
        return Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
    people = _people()
    return TimelineStore(people.registry, people.tokens).restamp(
        inputs.model_dump(exclude={'confirm', 'idempotency_key'}), actor=_actor.get(),
        idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)


def timeline_list(inputs: TimelineListInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.timeline import TimelineStore
    people = _people()
    try:
        return Done(result={'timelines': TimelineStore(people.registry, people.tokens).list(inputs.project)})
    except LookupError as error:
        return Refused(rule='project', reason=str(error))


class DrivePracticeOutput(BaseModel):
    practice: bool = True


class DriveMappingProposeInput(BaseModel):
    workbook: str
    tracker: str | None = None
    data_sources: Path
    file: str


class DriveMappingApplyInput(BaseModel):
    file: str
    idempotency_key: str | None = None
    confirm: str | None = None


class DriveMappingEntry(BaseModel):
    name: str
    project: str | None
    certain: bool
    project_name: str | None = None


class DriveMappingOutput(DrivePracticeOutput):
    mapping: list[DriveMappingEntry]


def drive_mapping_propose(inputs: BaseModel, open_notion: Callable[..., Any]) -> Outcome:
    from t6.cli import TOKEN_VARIABLE, _read_data_sources
    from t6.drives.mapping import propose_mapping
    from t6.drives.sheets import WorkbookSheetSource
    from t6.notion.reads import ProjectReader
    from t6.registry import ProjectRefs
    assert isinstance(inputs, DriveMappingProposeInput)
    token = os.environ.get(TOKEN_VARIABLE)
    if _notion_source.get() is None and not token:
        return Refused(rule='config', reason=f'Set {TOKEN_VARIABLE}')
    try:
        refs = ProjectRefs(_people().registry)
        projects = tuple(refs.identified(project) for project in ProjectReader(
            _verb_notion_source(open_notion, token or ''), _read_data_sources(inputs.data_sources)['projects']).list())
        registered = {project.t6_id: project for project in projects if project.t6_id}
        for project in _people().registry.list_projects():
            if project.id not in registered:
                registered[project.id] = Project(page_id=project.id, name='', t6_id=project.id,
                                                  project_code=project.code, rotation=project.rotation.value)
        mapping = propose_mapping(WorkbookSheetSource(inputs.workbook),
                                  WorkbookSheetSource(inputs.tracker) if inputs.tracker else None,
                                  tuple(registered.values()), inputs.file)
        return Done(result={'mapping': mapping})
    except (OSError, ValueError) as error:
        return Refused(rule='mapping', reason=str(error))


def drive_mapping_apply(inputs: BaseModel, open_notion: Callable[..., Any]) -> Outcome:
    from t6.drives.mapping import apply_mapping
    if not os.environ.get('T6_PREVIEW_SIGNING_KEY'):
        return Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
    assert isinstance(inputs, DriveMappingApplyInput)
    people = _people()
    try:
        return apply_mapping(people.registry, people.tokens, inputs.file, actor=_actor.get(),
                             idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)
    except (OSError, ValueError) as error:
        return Refused(rule='mapping', reason=str(error))


class DriveCaptureInput(BaseModel):
    chat_id: int | None = None
    topic_id: int | None = None
    message_id: int | None = None
    sender_telegram_id: int | None = None
    posted_at: AwareDatetime | None = None
    text: str = ''  # OUT person or IN [place]; optional label/serial after a newline or |
    photo: str | None = None  # base64-encoded file bytes
    media_type: Literal['image/png', 'image/jpeg', 'image/webp'] = 'image/png'
    capture: str | None = None


class DriveCaptureReadInput(BaseModel):
    capture: str


class DriveCaptureLine(BaseModel):
    label: str | None
    direction: Literal['IN', 'OUT']
    person: str | None


class DriveCaptureOutput(BaseModel):
    capture: str
    state: Literal['pending', 'resolved', 'exception', 'undone', 'ignored']
    lines: list[DriveCaptureLine]
    differences: list[str]
    photo_reference: str | None
    message: str | None = None
    practice: bool = True


def drive_capture(inputs: BaseModel, open_notion: Callable[..., Any]) -> Outcome:
    from t6.drives.capture import capture_post
    assert isinstance(inputs, DriveCaptureInput)
    return capture_post(_registry.get(), inputs)


def drive_capture_undo(inputs: BaseModel, open_notion: Callable[..., Any]) -> Outcome:
    from t6.drives.capture import undo_capture
    assert isinstance(inputs, DriveCaptureReadInput)
    return undo_capture(_registry.get(), inputs.capture)


def drive_capture_result(inputs: BaseModel, open_notion: Callable[..., Any]) -> Outcome:
    from t6.drives.capture import capture_result
    assert isinstance(inputs, DriveCaptureReadInput)
    return capture_result(_registry.get(), inputs.capture)


class DriveReloadInput(BaseModel):
    workbook: str | None = None
    tracker: str | None = None
    idempotency_key: str | None = None
    confirm: str | None = None


class DriveCutoverInput(DriveReloadInput):
    pass


class DriveWithoutSerial(BaseModel):
    id: str
    label: str | None


class DriveReloadOutput(DrivePracticeOutput):
    nightly_enabled: bool = True
    movements_read: int
    movements_matched: int
    movements_unmatched: int
    rows_read: int
    drives: int
    assignments: int
    released: int
    retired: int
    drives_without_serial: int
    without_serial: list[DriveWithoutSerial]
    exceptions: int
    unmapped_projects: list[str]


def drive_reload(inputs: BaseModel, open_notion: Callable[..., Any]) -> Outcome:
    from t6.drives.reload import reload_drives
    from t6.drives.sheets import SheetSource, WorkbookSheetSource, configured_sources
    if not os.environ.get('T6_PREVIEW_SIGNING_KEY'):
        return Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
    assert isinstance(inputs, DriveReloadInput)
    people = _people()
    from t6.drives.practice import practice_mode
    if not practice_mode(people.registry):
        return Refused(rule='cutover', reason='Drive reload is disabled after cutover')
    source: SheetSource
    tracker: SheetSource | None
    try:
        if inputs.workbook:
            source, tracker = (WorkbookSheetSource(inputs.workbook),
                               WorkbookSheetSource(inputs.tracker) if inputs.tracker else None)
        else:
            source, tracker = configured_sources(os.environ)
        return reload_drives(people.registry, people.tokens, source,
                             tracker=tracker,
                             actor=_actor.get(), idempotency_key=inputs.idempotency_key, confirm=inputs.confirm,
                             cutover=isinstance(inputs, DriveCutoverInput))
    except (OSError, ValueError) as error:
        return Refused(rule='sheet', reason=str(error))


class DriveProjectsInput(BaseModel):
    pass


class DriveProjectCount(BaseModel):
    project: DriveRef
    total: int
    in_house: int
    released: int


class DriveProjectsOutput(DrivePracticeOutput):
    projects: list[DriveProjectCount]


def drive_projects(inputs: BaseModel, open_notion: Callable[..., Any]) -> Outcome:
    from t6.drive_reads import project_counts
    from t6.drives.sqlite import SqliteDriveStore
    registry = _people().registry
    with registry._transaction() as db:
        return Done(result={'projects': project_counts(SqliteDriveStore(registry, within=db))})


class MovementCorrectInput(BaseModel):
    movement: str
    drive: str | None = None
    on: str | None = None
    direction: Literal['IN', 'OUT'] | None = None
    person: str | None = None
    place: Literal['TABLE', 'CABINET', 'SHELF', 'BOX'] | None = None
    idempotency_key: str | None = None
    confirm: str | None = None


class MovementCorrectOutput(DrivePracticeOutput):
    movement: MovementItem


def movement_correct(inputs: BaseModel, open_notion: Callable[..., Any]) -> Outcome:
    from t6.drives.movements import correct_movement
    assert isinstance(inputs, MovementCorrectInput)
    if not os.environ.get('T6_PREVIEW_SIGNING_KEY'):
        return Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
    changes = inputs.model_dump(exclude={'movement', 'idempotency_key', 'confirm'}, exclude_unset=True)
    if changes.get('direction', 'IN') is None or not changes:
        return Refused(rule='movement', reason='Give at least one correction; direction must be IN or OUT')
    if changes.get('on'):
        try:
            changes['on'] = IsoDate.fromisoformat(changes['on'])
        except ValueError:
            return Refused(rule='date', reason='Use an ISO date (YYYY-MM-DD)')
    people = _people()
    return correct_movement(people.registry, people.tokens, inputs.movement, changes, actor=_actor.get(),
                            idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)


class MovementListInput(BaseModel):
    needs_project: bool = False
    project: str | None = None
    drive: str | None = None
    person: str | None = None
    q: str | None = None
    start: str | None = None
    end: str | None = None


class MovementListOutput(DrivePracticeOutput):
    movements: list[MovementItem]


def movement_list(inputs: BaseModel, open_notion: Callable[..., Any]) -> Outcome:
    from t6.drives.movements import list_movements
    values = inputs.model_dump()
    try:
        for field in ('start', 'end'):
            values[field] = IsoDate.fromisoformat(values[field]) if values[field] else None
    except ValueError:
        return Refused(rule='date', reason='Use ISO dates (YYYY-MM-DD)')
    with _people().registry._transaction() as db:
        return Done(result={'movements': list_movements(db, **values)})


class DriveReceiveInput(BaseModel):
    drive: str
    project: str
    party: str | None = None
    party_kind: Literal['contact', 'studio'] | None = None
    on: str | None = None
    idempotency_key: str | None = None
    confirm: str | None = None


class DriveReturnInput(DriveReceiveInput):
    pass


class DriveReleaseInput(BaseModel):
    drives: list[str]
    project: str
    party: str
    party_kind: Literal['contact', 'studio']
    on: str | None = None
    delivery: str | None = None
    reason: str | None = None
    recipient: str | None = None  # the person who took the Drives, in free text
    idempotency_key: str | None = None
    confirm: str | None = None


class DriveRetireInput(BaseModel):
    drive: str
    project: str
    reason: str
    on: str | None = None
    idempotency_key: str | None = None
    confirm: str | None = None


class DriveAssignInput(BaseModel):
    drive: str
    project: str
    label: str
    role: DriveRole
    start: str | None = None
    end: str | None = None
    remarks: str | None = None
    idempotency_key: str | None = None
    confirm: str | None = None


class DriveUnassignInput(BaseModel):
    drive: str
    project: str | None = None  # needed unless `assignment` names the Assignment
    label: str | None = None
    role: DriveRole | None = None
    assignment: int | None = None
    end: str | None = None
    idempotency_key: str | None = None
    confirm: str | None = None


class DriveWriteOutput(DrivePracticeOutput):
    action: str
    drive_ids: list[str]
    project_id: str | None = None
    on: str | None = None
    shipment_id: str | None = None
    delivery_id: str | None = None
    label: str | None = None
    role: str | None = None
    assignment: int | None = None
    start: str | None = None
    end: str | None = None
    remarks: str | None = None
    idempotency_key: str


def drive_write(inputs: BaseModel, open_notion: Callable[..., Any]) -> Outcome:
    from t6.drive_verbs import DriveVerbs
    if not os.environ.get('T6_PREVIEW_SIGNING_KEY'):
        return Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
    people = _people()
    verb = next(name for model, name in ((DriveReceiveInput, 'receive'), (DriveReleaseInput, 'release'), (DriveReturnInput, 'return'),
                                         (DriveRetireInput, 'retire'), (DriveAssignInput, 'assign'), (DriveUnassignInput, 'unassign'))
                if type(inputs) is model)
    return DriveVerbs(people.registry, people.tokens).write(
        verb, inputs.model_dump(exclude={'idempotency_key', 'confirm'}, exclude_none=True), actor=_actor.get(),
        idempotency_key=getattr(inputs, 'idempotency_key'), confirm=getattr(inputs, 'confirm'))


class SwingAssignInput(BaseModel):
    drive: str
    target: str
    project: str | None = None
    label: str | None = None
    start: str | None = None
    return_date: str | None = None
    idempotency_key: str | None = None
    confirm: str | None = None


class SwingUnassignInput(BaseModel):
    drive: str
    project: str | None = None
    person: str | None = None
    assignment: int | None = None
    end: str | None = None
    idempotency_key: str | None = None
    confirm: str | None = None


class SwingReturnInput(SwingUnassignInput):
    pass


class SwingWriteOutput(DriveWriteOutput):
    project_id: str | None = None
    holder: str | None = None
    return_date: str | None = None


class SwingOverdueInput(BaseModel):
    pass


class SwingOverdueOutput(DrivePracticeOutput):
    today: str
    overdue: list[dict[str, Any]]


def swing_write(inputs: BaseModel, open_notion: Callable[..., Any]) -> Outcome:
    from t6.drive_verbs import DriveVerbs
    if not os.environ.get('T6_PREVIEW_SIGNING_KEY'):
        return Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
    people = _people()
    verb = next(name for model, name in ((SwingAssignInput, 'swing_assign'), (SwingUnassignInput, 'swing_unassign'), (SwingReturnInput, 'swing_return'))
                if type(inputs) is model)
    return DriveVerbs(people.registry, people.tokens).write(
        verb, inputs.model_dump(exclude={'idempotency_key', 'confirm'}, exclude_none=True), actor=_actor.get(),
        idempotency_key=getattr(inputs, 'idempotency_key'), confirm=getattr(inputs, 'confirm'))


def swing_overdue(inputs: SwingOverdueInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.drive_verbs import DriveVerbs
    people = _people()
    return Done(result=DriveVerbs(people.registry, people.tokens).overdue())


class DriveAddInput(BaseModel):
    label: str
    serial: str | None = None
    suspected_serial: str | None = None
    description: str | None = None
    brand: str | None = None
    model: str | None = None
    capacity_tb: float | None = None
    format: str | None = None
    form_factor: Literal['portable', 'powered desktop'] | None = None
    owner_studio_id: str | None = None
    ownership_known: bool = True
    purchase_date: str | None = None
    warranty: str | None = None
    remarks: str | None = None
    needs_review: bool = False
    review_note: str | None = None
    office_location: str | None = None  # where the Drive sits in the office, in the studio's own words
    qr_code: str | None = None  # the code on the Drive's label; one Drive holds it, in any case
    idempotency_key: str | None = None
    confirm: str | None = None


class DriveEditInput(BaseModel):
    drive: str
    label: str | None = None
    form_factor: Literal['portable', 'powered desktop', ''] | None = None
    serial: str | None = None
    suspected_serial: str | None = None
    needs_review: bool | None = None
    review_note: str | None = None
    description: str | None = None
    brand: str | None = None
    model: str | None = None
    capacity_tb: str | None = None  # TB as a number; blank clears
    format: str | None = None
    owner_studio_id: str | None = None
    ownership_known: bool | None = None
    purchase_date: str | None = None
    warranty: str | None = None
    remarks: str | None = None
    office_location: str | None = None
    qr_code: str | None = None
    expected_version: str | None = None
    idempotency_key: str | None = None
    confirm: str | None = None

    @field_validator('capacity_tb', mode='before')
    @classmethod
    def _capacity_number(cls, value: Any) -> Any:
        return str(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else value


class DrivePromoteSerialInput(BaseModel):
    drive: str
    idempotency_key: str | None = None
    confirm: str | None = None


class DriveFactsOutput(DrivePracticeOutput):
    action: str
    drive_ids: list[str]
    facts: dict[str, Any]
    idempotency_key: str


def drive_fact_write(inputs: BaseModel, open_notion: Callable[..., Any]) -> Outcome:
    from t6.drive_verbs import DriveVerbs
    if not os.environ.get('T6_PREVIEW_SIGNING_KEY'):
        return Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
    people = _people()
    verb = next(name for model, name in ((DriveAddInput, 'add'), (DriveEditInput, 'edit'), (DrivePromoteSerialInput, 'promote_serial'),
                                         (DriveCatalogSnapshotInput, 'catalog_snapshot'))
                if type(inputs) is model)
    return DriveVerbs(people.registry, people.tokens).write(
        verb, inputs.model_dump(exclude={'idempotency_key', 'confirm'}, exclude_none=True), actor=_actor.get(),
        idempotency_key=getattr(inputs, 'idempotency_key'), confirm=getattr(inputs, 'confirm'))


class DriveListInput(BaseModel):
    q: str | None = None
    serial: str | None = None
    needs_review: bool | None = None
    project: str | None = None
    status: Literal['in custody', 'released', 'retired', 'unknown'] | None = None
    office_location: str | None = None  # a Drive whose location contains this text, in any case
    limit: int | None = Field(default=None, ge=1)
    offset: int = Field(default=0, ge=0)


class DriveGetInput(BaseModel):
    drive: str


class DriveFindInput(BaseModel):
    serial: str | None = None
    qr_code: str | None = None

    @model_validator(mode='after')
    def _one_of(self) -> 'DriveFindInput':
        if bool((self.serial or '').strip()) == bool((self.qr_code or '').strip()):
            raise ValueError('Give either serial or qr_code')
        return self


class DriveShipmentsInput(BaseModel):
    pass


class DriveShipmentInput(BaseModel):
    shipment: str


class DriveExceptionsInput(BaseModel):
    pass


class DriveRef(BaseModel):
    page_id: str | None
    text: str | None


class MovementItem(BaseModel):
    recorder_telegram_id: int | None = None
    shipment_id: str | None = None
    posted_at: str | None = None
    id: str
    drive: str | None
    label: str | None
    on: str | None
    direction: Literal['IN', 'OUT']
    person: str | None
    recorder: str | None
    place: Literal['TABLE', 'CABINET', 'SHELF', 'BOX'] | None
    project: DriveRef | None
    origin: DriveSourceItem | None
    capture: str | None
    corrects: str | None
    description: str | None
    remarks: str | None


class DriveParty(BaseModel):
    kind: str
    ref: DriveRef


class DriveItem(BaseModel):
    """A Drive's facts and its status, derived from the custody log ('unknown' when there are no events)."""
    id: str
    label: str | None
    description: str | None
    serial: str | None
    suspected_serial: str | None
    needs_review: bool
    review_note: str | None
    brand: str | None
    model: str | None
    capacity_tb: float | None
    format: str | None
    form_factor: str | None
    ownership: str
    owner_studio_id: str | None
    purchase_date: str | None
    warranty: str | None
    remarks: str | None
    office_location: str | None
    qr_code: str | None
    version: str
    status: str


class DriveEventItem(BaseModel):
    kind: str
    on: str | None
    party: DriveParty | None
    project: DriveRef
    logged_by: DriveRef
    reason: str | None
    shipment_id: str | None


class DriveAssignmentItem(BaseModel):
    number: int
    label: str | None
    project: DriveRef | None
    role: str | None
    start: str | None
    end: str | None
    remarks: str | None
    return_date: str | None
    holder: str | None
    open: bool


class DriveSourceItem(BaseModel):
    workbook: str
    sheet: str
    row: int


class DriveListItem(DriveItem):
    open_assignments: list[DriveAssignmentItem]  # the Assignments not yet ended, numbered as `drive get` numbers them


class DriveListOutput(DrivePracticeOutput):
    drives: list[DriveListItem]
    total: int


class DriveGetOutput(DrivePracticeOutput):
    drive: DriveItem
    events: list[DriveEventItem]
    assignments: list[DriveAssignmentItem]
    sources: list[DriveSourceItem]
    deliveries: list[str]


class DriveMatch(DriveItem):
    match: Literal['accepted', 'suspected', 'qr_code']


class DriveFindOutput(DrivePracticeOutput):
    serial: str | None = None
    qr_code: str | None = None
    matches: list[DriveMatch]


class DriveCatalogSnapshotInput(BaseModel):
    """A dated scan of a Drive's contents. Each scan is kept as the changes since the one before it."""
    drive: str
    captured_on: str  # the day of the scan, YYYY-MM-DD; later than the Drive's last scan
    root_path: str = '/'  # what was scanned, such as the volume name
    entries: list[dict[str, Any]]  # each: path (relative, forward slashes), kind (file or dir), and for a file size_bytes, optional mtime and checksum
    idempotency_key: str | None = None
    confirm: str | None = None


class DriveCatalogSnapshotOutput(DrivePracticeOutput):
    action: str
    drive_ids: list[str]
    captured_on: str
    root_path: str
    parent_snapshot_id: str | None
    entry_count: int
    total_bytes: int
    added: int
    modified: int
    removed: int
    idempotency_key: str


class DriveCatalogSnapshotsInput(BaseModel):
    drive: str


class DriveCatalogDiffInput(BaseModel):
    drive: str
    snapshot: str


class DriveCatalogAtInput(BaseModel):
    drive: str
    on: str  # a day, YYYY-MM-DD: the tree as of the latest scan on or before it
    prefix: str | None = None  # only the paths under this folder


class DriveCatalogSnapshotItem(BaseModel):
    id: str
    captured_on: str
    parent_snapshot_id: str | None
    root_path: str
    entry_count: int
    total_bytes: int


class DriveCatalogSnapshotsOutput(DrivePracticeOutput):
    snapshots: list[DriveCatalogSnapshotItem]


class DriveCatalogChange(BaseModel):
    path: str
    kind: str
    change: Literal['added', 'modified', 'removed']
    size_bytes: int | None
    mtime: str | None
    checksum: str | None


class DriveCatalogDiffOutput(DrivePracticeOutput):
    snapshot: DriveCatalogSnapshotItem
    changes: list[DriveCatalogChange]


class DriveCatalogEntryItem(BaseModel):
    path: str
    kind: str
    size_bytes: int | None
    mtime: str | None
    checksum: str | None


class DriveCatalogAtOutput(DrivePracticeOutput):
    snapshot: DriveCatalogSnapshotItem
    entries: list[DriveCatalogEntryItem]


class DriveShipmentItem(BaseModel):
    shipment_id: str
    on: str | None
    party: DriveParty | None
    project: DriveRef
    drive_count: int


class DriveShipmentsOutput(DrivePracticeOutput):
    shipments: list[DriveShipmentItem]


class DriveRelease(BaseModel):
    drive: DriveItem
    event: DriveEventItem


class DriveShipmentOutput(DrivePracticeOutput):
    """`releases` hold each Drive's facts as they were at release, unless `facts_at_release` is false: the
    shipment was released before facts were kept, and they are the Drive's current facts."""
    shipment_id: str
    recipient: str | None
    facts_at_release: bool
    releases: list[DriveRelease]


class DriveExceptionItem(BaseModel):
    workbook: str
    sheet: str
    row: int
    reason: str
    detail: str


class DriveExceptionsOutput(DrivePracticeOutput):
    captures: list[DriveCaptureOutput] = Field(default_factory=list)
    exceptions: list[DriveExceptionItem]


class DriveOverdueInput(BaseModel):
    pass


class DriveOverdueItem(BaseModel):
    drive_id: str
    label: str | None
    serial: str | None
    person: str | None
    released_on: str
    days_out: int


class DriveOverdueOutput(DrivePracticeOutput):
    overdue: list[DriveOverdueItem]


def drive_read(inputs: BaseModel, open_notion: Callable[..., Any]) -> Outcome:
    from t6 import drive_reads
    from t6.drives.sqlite import SqliteDriveStore
    registry = _people().registry
    result: BaseModel | None
    with registry._transaction() as db:  # one transaction for the whole read: the store would open one for each Drive's status and Assignments
        store = SqliteDriveStore(registry, within=db)
        if isinstance(inputs, DriveOverdueInput):
            from datetime import datetime
            from zoneinfo import ZoneInfo
            result = drive_reads.overdue(store, datetime.fromtimestamp(registry._clock(), ZoneInfo('Asia/Manila')).date())
        elif isinstance(inputs, DriveListInput):
            result = drive_reads.list_drives(store, **inputs.model_dump())
        elif isinstance(inputs, DriveGetInput):
            result = drive_reads.get(store, inputs.drive)
            if result is None:
                return Refused(rule='drive', reason=f'Drive {inputs.drive} not found')
        elif isinstance(inputs, DriveFindInput):
            result = drive_reads.find(store, inputs.serial, inputs.qr_code)
        elif isinstance(inputs, DriveCatalogSnapshotsInput):
            result = drive_reads.catalog_snapshots(store, inputs.drive)
            if result is None:
                return Refused(rule='drive', reason=f'Drive {inputs.drive} not found')
        elif isinstance(inputs, DriveCatalogDiffInput):
            result = drive_reads.catalog_diff(store, inputs.drive, inputs.snapshot)
            if result is None:
                return Refused(rule='snapshot', reason=f'Drive {inputs.drive} has no snapshot {inputs.snapshot}')
        elif isinstance(inputs, DriveCatalogAtInput):
            try:
                day = IsoDate.fromisoformat(inputs.on)
            except ValueError:
                return Refused(rule='input', reason='on must be a date as YYYY-MM-DD')
            result = drive_reads.catalog_at(store, inputs.drive, day, inputs.prefix)
            if result is None:
                return Refused(rule='snapshot', reason=f'Drive {inputs.drive} had no scan by {inputs.on}')
        elif isinstance(inputs, DriveShipmentInput):
            result = drive_reads.shipment(store, inputs.shipment)
            if result is None:
                return Refused(rule='shipment', reason=f'Shipment {inputs.shipment} not found')
        elif isinstance(inputs, DriveShipmentsInput):
            result = drive_reads.shipments(store)
        else:
            from t6.drives.capture import _result
            result = drive_reads.exceptions(store)
            practice = bool(db.execute('SELECT practice FROM drive_practice WHERE id=1').fetchone()[0])
            result.captures = [DriveCaptureOutput(**_result(data), practice=practice)
                               for row in db.execute('SELECT data FROM drive_captures ORDER BY rowid')
                               if (data := json.loads(row[0]))['state'] == 'exception']
    return Done(result=result.model_dump(mode='json'))


class TranscriptCreateInput(BaseModel):
    media: str
    deliverable: str | None = None
    manifest: str | None = None
    language: str = 'fil'
    no_diarize: bool = False
    formats: list[str] = Field(default_factory=lambda: ['srt', 'docx'])
    start_timecode: str | None = None
    max_hours: float | None = None
    idempotency_key: str | None = None
    confirm: str | None = None


class TranscriptSpeakersInput(BaseModel):
    transcript: str
    csv: str
    idempotency_key: str | None = None
    confirm: str | None = None


class TranscriptTranslateInput(BaseModel):
    transcript: str
    idempotency_key: str | None = None
    confirm: str | None = None


class TranscriptGetInput(BaseModel):
    transcript: str


class TranscriptCreateOutput(BaseModel):
    job_id: int
    transcript_id: str | None = None


class TranscriptGetOutput(BaseModel):
    transcript: dict[str, Any]


def transcript_get(inputs: TranscriptGetInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.transcript import TranscriptStore
    result = TranscriptStore(_people().registry).get(inputs.transcript)
    return Done(result={'transcript': result}) if result else Refused(rule='transcript', reason='Transcript not found')

class AbangOutput(BaseModel):
    request_id: str
    state: str


def abang_write(inputs: BaseModel, open_notion: Callable[..., Any]) -> Outcome:
    from t6.abang import Abang
    from t6.preview import PreviewTokens
    key = os.environ.get('T6_PREVIEW_SIGNING_KEY', '')
    if not key:
        return Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
    assert isinstance(inputs, (AbangRequestInput, AbangCancelInput, AbangAcceptInput))
    verb = 'request' if isinstance(inputs, AbangRequestInput) else 'accept' if isinstance(inputs, AbangAcceptInput) else 'cancel'
    values = inputs.model_dump(exclude={'confirm', 'idempotency_key'})
    return Abang(_registry.get(), PreviewTokens(key)).write(verb, values, actor=_actor.get(),
        idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)


VERBS = (
    Verb('transcript', 'create', 'Create a Transcript', (), TranscriptCreateInput, TranscriptCreateOutput, 'staff', 'Preview and confirm a transcription job.', api_read),
    Verb('transcript', 'speakers', 'Name Transcript speakers', (), TranscriptSpeakersInput, TranscriptCreateOutput, 'staff', 'Preview and confirm speaker names.', api_read),
    Verb('transcript', 'translate', 'Translate a Transcript', (), TranscriptTranslateInput, TranscriptCreateOutput, 'staff', 'Preview and confirm translation.', api_read),
    Verb('transcript', 'get', 'Get a Transcript', (), TranscriptGetInput, TranscriptGetOutput, 'reader', 'Read progress and summary.', transcript_get, read_only=True),
    Verb('drive', 'mapping propose', 'Write proposed sheet Project mappings for review', (), DriveMappingProposeInput, DriveMappingOutput, 'drives', 'Propose exact names, then codes, then fuzzy names; never store mappings.', drive_mapping_propose),
    Verb('drive', 'mapping apply', 'Store a checked sheet Project mapping file', (), DriveMappingApplyInput, DriveMappingOutput, 'drives', 'Preview and confirm mappings to registered T6 Project IDs. Null removes a mapping.', drive_mapping_apply),
    Verb('drive', 'projects', 'Project Drive counts', (), DriveProjectsInput, DriveProjectsOutput, 'reader', 'Distinct Drives per Project with an open Assignment, grouped by derived custody status.', drive_projects, read_only=True),
    Verb('drive', 'capture', 'Store a recording-channel post', (), DriveCaptureInput, DriveCaptureOutput, 'gateway', 'Gateway only; stores first and queues resolution.', drive_capture),
    Verb('drive', 'capture undo', 'Undo a capture', (), DriveCaptureReadInput, DriveCaptureOutput, 'gateway', 'Gateway only; appends reversing entries once.', drive_capture_undo),
    Verb('drive', 'capture result', 'Read a capture outcome', (), DriveCaptureReadInput, DriveCaptureOutput, 'reader', 'Includes Practice and ask-for-label state.', drive_capture_result, read_only=True),
    Verb('movement', 'correct', 'Append a Movement correction', (), MovementCorrectInput, MovementCorrectOutput, 'drives', 'Preview and confirm a correction; the original Movement and custody events remain.', movement_correct),
    Verb('movement', 'list', 'Search the Movement log', (), MovementListInput, MovementListOutput, 'reader', 'Filters combine with AND; includes unresolved rows and corrections.', movement_list, read_only=True),
    Verb('drive', 'cutover', 'Final reload and end Drive practice mode', (), DriveCutoverInput, DriveReloadOutput, 'operator', 'Preview and confirm the final reload; disables nightly and operator reloads.', drive_reload),
    Verb('drive', 'reload', 'Replace Drive data from the per-project sheet', (), DriveReloadInput, DriveReloadOutput, 'drives', 'Preview and confirm a complete reload, preserving accepted serial IDs.', drive_reload),
    Verb('drive', 'receive', 'Receive a Drive into custody', (), DriveReceiveInput, DriveWriteOutput, 'drives', 'Preview and confirm; appends a received event. Refused for a Drive in custody or retired.', drive_write),
    Verb('drive', 'release', 'Release Drives in one shipment', (), DriveReleaseInput, DriveWriteOutput, 'drives', 'Preview and confirm; one released event per Drive, grouped by shipment, with the Drives\' facts kept as they are now. Optional reason and free-text recipient. Refused whole if a Drive is released or retired.', drive_write),
    Verb('drive', 'return', 'Return a Drive to custody', (), DriveReturnInput, DriveWriteOutput, 'drives', 'Preview and confirm; appends a returned event. Only for a released Drive.', drive_write),
    Verb('drive', 'retire', 'Retire a Drive', (), DriveRetireInput, DriveWriteOutput, 'drives', 'Needs a reason; refused for a Drive already retired.', drive_write),
    Verb('drive', 'assign', 'Assign a Drive to a Project', (), DriveAssignInput, DriveWriteOutput, 'drives', 'Preview and confirm; adds an Assignment with label, role and optional remarks.', drive_write),
    Verb('drive', 'unassign', 'End a Drive Assignment', (), DriveUnassignInput, DriveWriteOutput, 'drives', 'Ends the open Assignment and keeps it in history; when several are open, pick by label, role or assignment number. The number alone is enough, and the only way to end one whose Project is text only.', drive_write),
    Verb('drive', 'add', 'Register a Drive', (), DriveAddInput, DriveFactsOutput, 'drives', 'Preview and confirm; a serial already recorded is refused, naming the Drive that holds it.', drive_fact_write),
    Verb('drive', 'edit', 'Correct the facts of a Drive', (), DriveEditInput, DriveFactsOutput, 'drives', 'Preview and confirm; changes only the facts named, and an empty value clears one. expected_version (from drive get or list) refuses the edit if the Drive changed since. Custody events and Assignments are not touched.', drive_fact_write),
    Verb('drive', 'promote-serial', 'Accept the suspected serial of a Drive', (), DrivePromoteSerialInput, DriveFactsOutput, 'drives', 'Preview and confirm; the suspected serial becomes the accepted one, only if no other Drive holds it.', drive_fact_write),
    Verb('drive', 'catalog-snapshot', 'Record a dated scan of a Drive\'s contents', (), DriveCatalogSnapshotInput, DriveCatalogSnapshotOutput, 'drives', 'Preview and confirm; send the whole manifest both times (the token binds it). Only what changed since the Drive\'s last scan is kept, and the preview counts added, modified and removed paths. A scan must be dated after the last one. A scan can be large (about 8,000 files is 1 to 2 MB), so this verb accepts a request up to 16 MB on the tailnet listener only.', drive_fact_write),
    Verb('drive', 'catalog-snapshots', 'List the scans of a Drive', (), DriveCatalogSnapshotsInput, DriveCatalogSnapshotsOutput, 'reader', 'Oldest first, each with its date, entry count and total bytes.', drive_read, read_only=True),
    Verb('drive', 'catalog-diff', 'Show what one scan of a Drive changed', (), DriveCatalogDiffInput, DriveCatalogDiffOutput, 'reader', 'The paths added, modified or removed by the snapshot, in path order.', drive_read, read_only=True),
    Verb('drive', 'catalog-at', 'Show what a Drive held on a day', (), DriveCatalogAtInput, DriveCatalogAtOutput, 'reader', 'The whole tree as of the latest scan dated on or before the day, in path order; prefix limits it to one folder. Refused when the Drive had no scan by then.', drive_read, read_only=True),
    Verb('drive', 'list', 'List Drives with their custody status', (), DriveListInput, DriveListOutput, 'reader', 'Filters (q, serial, needs_review, project, status, office_location) combine with AND; optional limit and offset page the result and total gives the count before paging. project is a T6 Project ID with an open Assignment. Each Drive carries its version and open Assignments.', drive_read, read_only=True),
    Verb('drive', 'get', 'Show one Drive', (), DriveGetInput, DriveGetOutput, 'reader', 'Facts, status, custody events in append order, numbered Assignments, source rows and linked Deliveries.', drive_read, read_only=True),
    Verb('drive', 'find', 'Find Drives by serial', (), DriveFindInput, DriveFindOutput, 'reader', 'Give a serial or a qr_code. A serial matches an accepted or a suspected one, in any spelling; a QR code matches in any case.', drive_read, read_only=True),
    Verb('drive', 'shipments', 'List Drive shipments', (), DriveShipmentsInput, DriveShipmentsOutput, 'reader', 'One entry for each shipment, in the order first released.', drive_read, read_only=True),
    Verb('drive', 'shipment', 'Show one Drive shipment', (), DriveShipmentInput, DriveShipmentOutput, 'reader', 'The Drives of the shipment with their release events and the recipient. Facts are as they were at release; facts_at_release is false for an earlier shipment, which shows current facts.', drive_read, read_only=True),
    Verb('drive', 'exceptions', 'List the Drive import exceptions', (), DriveExceptionsInput, DriveExceptionsOutput, 'reader', 'Rows the Drive import could not read, kept for review.', drive_read, read_only=True),
    Verb('drive', 'overdue', 'List T6 Drives OUT for more than 14 days', (), DriveOverdueInput, DriveOverdueOutput, 'reader', 'Last custody event must be a dated release more than 14 Manila days ago; oldest first.', drive_read, read_only=True),
    Verb('swing', 'assign', 'Assign a swing Drive to a Person or Project', (), SwingAssignInput, SwingWriteOutput, 'drives', 'Preview and confirm; adds a swing Assignment, with an optional return date. A Person target needs no Project; --project may be added.', swing_write),
    Verb('swing', 'unassign', 'End a swing Assignment', (), SwingUnassignInput, SwingWriteOutput, 'drives', 'Ends the open swing Assignment and keeps it, with its return date, in history. When several are open, pick by --project, --person or --assignment.', swing_write),
    Verb('swing', 'return', 'Return a swing Drive', (), SwingReturnInput, SwingWriteOutput, 'drives', 'Ends the open swing Assignment and clears its return date.', swing_write),
    Verb('swing', 'overdue', 'List swing Drives past their return date', (), SwingOverdueInput, SwingOverdueOutput, 'reader', 'Open swing Assignments due back before today (Asia/Manila).', swing_overdue, read_only=True),
    Verb('media', 'link', 'Link a Media location', (), MediaLinkInput, MediaWriteOutput, 'coordinator', 'Preview and confirm the link.', media_write),
    Verb('media', 'unlink', 'Unlink a Media location', (), MediaUnlinkInput, MediaWriteOutput, 'coordinator', 'Preview and confirm the unlink.', media_write),
    Verb('frameio', 'project link', 'Link a Frame.io project to a Project', (), FrameioProjectInput, MediaWriteOutput, 'staff', 'One Frame.io project belongs to one Project. Preview and confirm.', frameio_project_link),
    Verb('frameio', 'project unlink', 'Unlink a Frame.io project from a Project', (), FrameioProjectInput, MediaWriteOutput, 'staff', 'Moving is unlink then link. Preview and confirm.', frameio_project_unlink),
    Verb('timeline', 'link', 'Link a Resolve timeline and stamp it by code', (), TimelineLinkInput, TimelineWriteOutput, 'staff', 'The Resolve worker writes the stamp; one Timeline per stamp. Without --stamp t6 creates the stamp and the preview shows it. Confirm records the Timeline pending and queues one stamp job; the job marks it stamped or failed with the reason. Linking a failed Timeline again queues it again. Preview and confirm.', timeline_write),
    Verb('timeline', 'restamp', 'Give a duplicated timeline a new stamp', (), TimelineRestampInput, TimelineRestampOutput, 'staff', 'For a copy carrying the stamp of a Timeline of the Project: t6 creates a new stamp, records the copy as a pending Timeline and queues one stamp job that replaces the old stamp on it. The canonical Timeline keeps its stamp. Preview and confirm.', timeline_restamp),
    Verb('timeline', 'list', 'List Timelines of a Project', (), TimelineListInput, TimelineListOutput, 'reader', 'Read Timelines with their canonical copy and stamp state (pending, stamped, failed with the reason).', timeline_list, read_only=True),
    Verb('media', 'list', 'List Media locations', (), MediaListInput, MediaListOutput, 'reader', 'Read Media locations.', media_list, read_only=True),
    Verb('bay', 'book', 'Book a Bay', (), BayBookInput, BookingOutput, 'bookings', 'Overlaps require force.', booking_write),
    Verb('bay', 'list', 'List Bays', (), BayListInput, BayListOutput, 'reader', 'Read the Bays in the registry: each Bay ID with the page ID of its Notion row. Names stay in Notion.', bay_list, read_only=True),
    Verb('bay', 'scan report', 'Post a bay scan report', (), BayScanReportInput, BayScanReportOutput, 'bay_scan', 'Ties the report to a Bay through the machine serial and the Machine to Bays mapping; refused with rule no_bay when none matches. The latest scan per Bay is kept; a repeat or older scan changes nothing. Only the bay-scanner service may post.', api_write),
    Verb('bay', 'scan list', 'List bay scan reports', (), BayScanListInput, BayScanListOutput, 'reader', 'Each Bay with its last report and whether it is fresh or stale (older than 3 days). A Bay with no report is listed as no_report, never as having no local copies. Optional bay (ID or page ID).', bay_scan_list, read_only=True),
    Verb('bay', 'drift', 'Compare each bay\'s local copies with the project server', (), BayDriftInput, BayDriftOutput, 'reader', 'Per project on the server: which Bays hold a local copy and whether it stands equal, local_newer, local_older or differs, is not_on_server or report_stale, or cannot_compare. A Bay with no report is listed under unknown_bays, never as having no copies. Optional project (its Resolve-project Media location). Reports only; nothing is written.', api_read, read_only=True),
    Verb('booking', 'move', 'Move a Booking', (), BookingMoveInput, BookingOutput, 'bookings', 'Overlaps require force.', booking_write),
    Verb('booking', 'extend', 'Extend a Booking', (), BookingExtendInput, BookingOutput, 'bookings', 'Overlaps require force.', booking_write),
    Verb('booking', 'cancel', 'Cancel a Booking', (), BookingCancelInput, BookingOutput, 'bookings', 'Cancel an active Booking.', booking_write),
    Verb('booking', 'get', 'Get a Booking', (), BookingGetInput, BookingGetOutput, 'reader', 'Read a Booking.', booking_read, read_only=True),
    Verb('booking', 'list', 'List Bookings', (), BookingListInput, BookingListOutput, 'reader', 'Read Bookings.', booking_read, read_only=True),
    Verb("version", "get", "Get a Version", (), VersionGetInput, VersionFindOutput, "reader", "Read a Version and its Approvals.", api_read, read_only=True),
    Verb('abang', 'request', 'Request a file relay', (), AbangRequestInput, AbangOutput, 'coordinator', 'Request a file from a vendor.', abang_write),
    Verb('abang', 'accept', 'Decide an abang candidate', (), AbangAcceptInput, AbangOutput, 'person', 'Only the freelancer may decide.', abang_write),
    Verb('abang', 'cancel', 'Cancel a file relay', (), AbangCancelInput, AbangOutput, 'coordinator', 'Cancel a pending relay.', abang_write),
    Verb("version", "find", "Find a Version by Frame.io file", (), VersionFindInput, VersionFindOutput, "reader", "Resolve a review file to its Version.", api_read, read_only=True),
    Verb("version", "add", "Add a Version", (), VersionAddInput, VersionOutput, "coordinator", "Preview and confirm a Frame.io file.", version_write),
    Verb("version", "lock", "Lock a Version", (), VersionLockInput, VersionOutput, "coordinator", "Record picture lock.", version_write),
    Verb('approval', 'request', 'Request an Approval', tuple('approval request-' + gate for gate in ('vfx-committee', 'client-cut', 'grade', 'delivery-sign-off', 'drive-wipe', 'fleet-promotion', 'project-start')), ApprovalRequestInput, ApprovalOutput, 'person', 'An active Person may request an Approval.', approval_write),
    Verb('approval', 'grant', 'Grant an Approval', tuple('approval grant-' + gate for gate in ('vfx-committee', 'client-cut', 'grade', 'delivery-sign-off', 'drive-wipe', 'fleet-promotion', 'project-start')), ApprovalGrantInput, ApprovalOutput, 'person', 'The allowed-tapper rule applies.', approval_write),
    Verb('approval', 'deny', 'Deny an Approval', tuple('approval deny-' + gate for gate in ('vfx-committee', 'client-cut', 'grade', 'delivery-sign-off', 'drive-wipe', 'fleet-promotion', 'project-start')), ApprovalDenyInput, ApprovalOutput, 'person', 'The allowed-tapper rule and a reason are required.', approval_write),
    Verb("studio", "notices", "Read studio notices", (), StudioNoticesInput, StudioNoticesOutput, "reader", "Read Project source notices.", api_read, read_only=True),
    Verb("external-link", "find", "Find an External Link", (), ExternalLinkFindInput, ExternalLinkFindOutput, "reader", "Resolve an external reference.", api_read, read_only=True),
    Verb("project", "get", "Get a Project", (), ProjectGetInput, ProjectGetOutput, "reader", "Read a Project.", api_read, read_only=True),
    Verb("project", "notes", "Read dated PM Notes", (), ProjectNotesInput, ProjectNotesOutput, "reader", "Read dated PM Notes lines.", api_read, read_only=True),
    Verb("project", "notes-history", "Read a Project's PM Notes history", (), ProjectNotesHistoryInput, ProjectNotesHistoryOutput, "reader", "Every dated line the autoscan has archived for the Project (its t6 ID), newest first, with first seen, last seen and removed (the first run the line was absent) as Manila dates. Only Closers and the Project's coordinators may read it.", api_read, read_only=True),
    Verb("project", "flags", "Read Project flags", (), ProjectFlagsInput, ProjectFlagsOutput, "reader", "Recompute data gaps and date warnings.", api_read, read_only=True),
    Verb("person", "list", "List Contacts", (), PersonListInput, PersonListOutput, "reader", "Read Contacts.", api_read, read_only=True),
    Verb('text', 'withhold', 'Withhold credentials from text', (), TextWithholdInput, TextWithholdOutput, 'reader', 'Give up to 200 texts; each comes back in order with labelled credential values (password, passcode, pass, pin, pw) replaced by the placeholder. Nothing is stored. Use it before showing message text outside its chat.', text_withhold, read_only=True),
    Verb("standing-cc", "", "Get standing cc", (), StandingCcInput, StandingCcOutput, "reader", "Read configured standing cc.", None, read_only=True),
    Verb("jo", "list", "List Job Orders", (), JoListInput, JoListOutput, "reader", "Filter by Project, assignee or status group.", api_read, read_only=True),
    Verb("jo", "find", "Find Job Orders by Telegram Key, chat and message ID, or duplicate key", (), JoFindInput, JoListOutput, "reader", "Return all matching orders.", api_read, read_only=True),
    Verb("deliverable", "milestone record", "Record a Deliverable milestone", (), MilestoneRecordInput, MilestoneRecordOutput, "coordinator", "Milestones are append-only.", milestone_record),
    Verb("deliverable", "miss", "Record a missed Deliverable", (), DeliverableMissInput, DeliverableScheduleOutput, "coordinator", "A delivered Deliverable cannot be missed.", deliverable_schedule),
    Verb("deliverable", "delay", "Move a Deliverable date", (), DeliverableDelayInput, DeliverableScheduleOutput, "coordinator", "Date moves require a reason.", deliverable_schedule),
    Verb("deliverable", "deliver", "Deliver to the client", (), DeliverableDeliverInput, DeliverableDeliverOutput, "coordinator", "Blocking checklist items must be checked.", deliverable_deliver),
    Verb("deliverable", "define", "Define a Deliverable", (), DeliverableDefineInput, DeliverableDefineOutput, "operator", "Preview and confirm a Deliverable.", deliverable_define),
    Verb("deliverable", "edit", "Change a Deliverable code or episode, or give it its first dates", (), DeliverableEditInput, DeliverableEditOutput, "operator", "Names only the fields to change. A due or air date is set only when the Deliverable has none; moving an existing date is deliverable delay. A code may repeat inside a Project. The Frame.io folder keeps its old name: the Media location points at it by ID, so the link stays valid.", deliverable_edit),
    Verb("deliverable", "rebind", "Bind a replacement Deliverables page", (), DeliverableRebindInput, DeliverableDefineOutput, "operator", "Retain the old page as a legacy binding.", deliverable_rebind),
    Verb("deliverable", "get", "Get a Deliverable", (), DeliverableGetInput, DeliverableGetOutput, "reader", "Read from t6.", api_read, read_only=True),
    Verb("deliverable", "list", "List Project Deliverables", (), DeliverableListInput, DeliverableListOutput, "reader", "Read from t6.", api_read, read_only=True),
    Verb("deliverable", "candidates", "List every Deliverable page of a Project", (), DeliverableCandidatesInput, DeliverableCandidatesOutput, "reader", "Read from Notion: registered or not, one entry for each page, with name, Status and Class.", api_read, read_only=True),
    Verb("deliverable", "bangko", "List Deliverables ready ahead of air", (), FloorReadInput, FloorReadOutput, "reader", "Ready (kasado or delivered) at least the configured days before air; optional project.", floor_bangko, read_only=True),
    Verb("deliverable", "hand-to-mouth", "List Deliverables ready at the last moment", (), FloorReadInput, FloorReadOutput, "reader", "Ready within the configured days of air; optional project.", floor_hand_to_mouth, read_only=True),
    Verb("deliverable", "sabit", "List missed Deliverables", (), FloorReadInput, FloorReadOutput, "reader", "A recorded miss, not a delay; optional project.", floor_sabit, read_only=True),
    Verb("meeting-notes", "list", "List Project Meeting Notes", (), MeetingNotesListInput, MeetingNotesListOutput, "reader", "Read from Notion.", api_read, read_only=True),
    Verb("meeting-notes", "create", "Create a Meeting Notes page", (), MeetingNotesCreateInput, MeetingNotesWriteOutput, "minutes", "Not idempotent: look for the title with list first. A failed body is ambiguous with the page ID.", api_write),
    Verb("meeting-notes", "append", "Append blocks to a Meeting Notes page", (), MeetingNotesAppendInput, MeetingNotesWriteOutput, "minutes", "Refused when the note is sent or in the trash.", api_write),
    Verb("meeting-notes", "trash", "Trash a Meeting Notes page", (), MeetingNotesTrashInput, MeetingNotesWriteOutput, "minutes", "Refused when the note is sent.", api_write),
    Verb("meeting-notes", "mark-sent", "Set Sent at on a Meeting Notes page", (), MeetingNotesMarkSentInput, MeetingNotesWriteOutput, "minutes", "A note already sent keeps its first date.", api_write),
    Verb("project", "list", "List Projects", (), ProjectListInput, ProjectListOutput, "reader", "Read Projects.", api_read, read_only=True),
    Verb("llm", "run", "Run an LLM task on the API", (), LLMRunInput, LLMRunOutput, "llm", "Server policy and masking apply.", llm_api_only),
    Verb("llm", "decide", "Ask the Decisions provider for match, maybe or no on each candidate", (), LLMDecideInput, LLMDecideOutput, "llm", "Server policy and masking apply.", llm_decide_api_only),
    Verb("llm", "filter", "Ask the Decisions provider whether one message passes a named filter rule", (), LLMFilterInput, LLMFilterOutput, "llm", "Server policy and masking apply. The verdict is advice; a refusal means go on.", llm_filter_api_only),
    Verb("person", "add", "Register a Person", (), PersonAddInput, PersonWriteOutput, "operator", "A page belongs to one Person.", person_write),
    Verb("machine", "list", "List the Machines and their fleet facts", (), MachineListInput, MachineListOutput, "reader", "Read the Bays rows as Machines.", api_read, read_only=True),
    Verb("machine", "sync", "Write fleet facts to the Machines' Bays rows", (), MachineSyncInput, MachineSyncOutput, "fleet", "Preview each Machine's row and old and new facts, then confirm. Never creates a row.", api_write),
    Verb("machine", "edit", "Edit other Bays properties on the Machines' rows", (), MachineEditInput, MachineEditOutput, "bays_edit", "Preview each Machine's row and old and new values for the editable properties, then confirm. Never creates a row; nothing is cleared.", api_write),
    Verb("person", "create", "Create a Contact and register the Person", (), PersonCreateInput, PersonCreateOutput, "operator", "For someone with no Contacts page yet; an active Person uniquely holds a Telegram ID.", api_write),
    Verb("person", "rebind", "Bind a replacement Contacts page", (), PersonRebindInput, PersonWriteOutput, "operator", "A page belongs to one Person.", person_write),
    Verb("person", "set-telegram", "Set a Person Telegram ID", (), PersonTelegramInput, PersonWriteOutput, "operator", "An active Person uniquely holds a Telegram ID.", person_write),
    Verb("person", "shift", "Set a Person shift", (), PersonShiftInput, PersonWriteOutput, "operator", "Set a day or night shift.", person_write),
    Verb("person", "classify", "Classify a Person", (), PersonClassifyInput, PersonWriteOutput, "operator", "Set the reviewed Person kind.", person_write),
    Verb("person", "grant", "Grant a permission", (), PersonPermissionInput, PersonWriteOutput, "operator", "Only an operator or admin may grant permissions.", person_write),
    Verb("person", "revoke", "Revoke a permission", (), PersonPermissionInput, PersonWriteOutput, "operator", "Only an operator or admin may revoke permissions.", person_write),
    Verb("person", "assign", "Assign a Project role", (), PersonProjectRoleInput, PersonWriteOutput, "coordinator", "Assign a Person to a Project role.", person_assign),
    Verb("person", "unassign", "Remove a Project role", (), PersonProjectRoleInput, PersonWriteOutput, "coordinator", "Remove a Person from a Project role.", person_unassign),
    Verb("person", "get", "Get a Person", (), PersonGetInput, PersonGetOutput, "reader", "Read a Person.", person_get, read_only=True),
    Verb("project", "link-chat", "Link a Telegram chat to a Project", (), ProjectChatInput, ProjectChatOutput, "coordinator", "Approval is required.", project_link_chat),
    Verb("project", "unlink-chat", "Unlink a Telegram chat from a Project", (), ProjectChatInput, ProjectChatOutput, "coordinator", "Approval is required.", project_unlink_chat),
    Verb("project", "start", "Start a Project: a previewed plan, then its t6 steps", (), ProjectStartInput, ProjectStartOutput, "operator", "A preview by job; confirm requests the Approval and the Start waits. When the Approval is granted the Frame.io step, Deliverables, Media locations and Bookings are applied once, as the Person who confirmed; a denied Approval applies nothing. The manual steps are listed and recorded then.", project_start),
    Verb("project", "archive", "Archive a Project", (), ProjectResumeInput, ProjectWriteOutput, "coordinator", "An archived Project cannot accept new work.", project_archive),
    Verb("project", "hold", "Hold a Project", (), ProjectHoldInput, ProjectWriteOutput,
         "operator", "A held Project cannot be held again.", project_hold),
    Verb("project", "resume", "Resume a held Project", (), ProjectResumeInput, ProjectWriteOutput,
         "operator", "Only a held Project can be resumed.", project_resume),
    Verb("project", "activity", "Show Project activity, newest first", (),
         ActivityInput, ActivityOutput, "reader", "Activity is append-only.", project_activity, read_only=True),
    Verb('package', 'list', 'List open Packages', (), PackageListInput, PackageListOutput, 'reader', 'Filter to Packages with missing items.', package_list, read_only=True),
    Verb('queue', 'failed', 'List the failed internal jobs', (), QueueFailedInput, QueueFailedOutput, 'operator', 'The dead jobs of the internal queue in the t6 database, not the webhook jobs on the VPS edge (see `t6 admin queue`).', queue_failed, read_only=True),
    Verb('queue', 'dismiss', 'Dismiss a failed internal job', (), QueueDismissInput, QueueDismissOutput, 'operator', 'Preview and confirm; a reason is required and the dismissal is audited. A dismissed job is never run again and no longer counts as failed. Covers the internal queue in the t6 database only.', queue_dismiss),
    Verb('package', 'get', 'Get a Package', (), PackageGetInput, PackageGetOutput, 'reader', 'Read a Package and its checklist.', package_get, read_only=True),
    Verb('package', 'prepare', 'Prepare a Package', (), PackagePrepareInput, PackageOutput, 'person', 'Copy the Package Spec.', package_write),
    Verb('package', 'check', 'Check a Package item', (), PackageCheckInput, PackageOutput, 'person', 'Record who checked the item and when.', package_write),
    Verb('package', 'handoff', 'Hand off a Package', (), PackageHandoffInput, PackageOutput, 'person', 'Blocking items must be checked.', package_write),
    Verb('package', 'accept', 'Accept a Package', (), PackageAcceptInput, PackageOutput, 'person', 'The recipient accepts. A Package for the client ends at handoff and is refused.', package_write),
    Verb('package', 'reject', 'Reject a Package', (), PackageRejectInput, PackageOutput, 'person', 'The recipient gives a reason. A Package for the client ends at handoff and is refused.', package_write),
    Verb("concern", "for-message", "Find Concerns carried by a brief", (), ConcernForMessageInput, ConcernForMessageOutput, "reader", "Return Concern IDs for a brief message.", concern_for_message, read_only=True),
    Verb("concern", "record-message", "Record a Concern carried by a brief", (), ConcernRecordMessageInput, ConcernOutput, "coordinator", "Record a brief message reference without chat text.", concern_write),
    Verb("concern", "scan", "Scan PM Notes for Concerns", (), ConcernScanInput, ConcernScanOutput, "coordinator", "Match new dated lines through llm run.", concern_scan),
    Verb("concern", "observe", "Hand over the findings of one brief run", (), ConcernObserveInput, ConcernObserveOutput, "coordinator", "The API decides which findings are new, known or not seen; it closes nothing unless T6_CONCERN_OBSERVE_CLOSES is on.", concern_observe),
    Verb("concern", "counts", "Count the Concerns each PSR listed", (), ConcernCountsInput, ConcernCountsOutput, "reader", "Counts only: listed, sent, new, closed, ages and sources for each run date.", concern_counts, read_only=True),
    Verb("concern", "list", "List Concerns to raise at PSR", (), ConcernListInput, ConcernListOutput, "reader", "With raise_at_psr: every Concern that is not closed, oldest evidence first, with owner, waiting on, age and the date of its last evidence. Age never decides inclusion. Both modes also return `pending`: the Concerns with a pending close, only there, with who closed them and when.", concern_list, read_only=True),
    Verb("concern", "changes", "Read automatic Concern changes", (), ConcernChangesInput, ConcernChangesOutput, "reader", "Read receipts since a date.", concern_changes, read_only=True),
    Verb("concern", "get", "Get a Concern", (), ConcernGetInput, ConcernGetOutput, "reader", "Read a Concern and its age.", concern_get, read_only=True),
    Verb("concern", "raise", "Raise a Concern", (), ConcernRaiseInput, ConcernOutput, "coordinator", "Record a line waiting on a party.", concern_write),
    Verb("concern", "note", "Add a Concern note", (), ConcernNoteInput, ConcernOutput, "coordinator", "Notes are append-only.", concern_write),
    Verb("concern", "resolve", "Resolve a Concern", (), ConcernResolveInput, ConcernOutput, "coordinator", "A Closer's close is pending until the next PSR: the Monday autoscan applies it unless a PM Notes line continues the Concern first. Reopen cancels it.", concern_write),
    Verb("concern", "reopen", "Reopen a Concern", (), ConcernReopenInput, ConcernOutput, "coordinator", "Keep its original age.", concern_write),
    Verb("jo", "get", "Get a Job Order", (), JoGetInput, JoGetOutput, "reader", "Read a Job Order.", None, read_only=True),
    Verb("jo", "raise", "Raise a Job Order", (), JoRaiseInput, JoWriteOutput, "coordinator", "Create a Job Order.", jo_write),
    Verb("jo", "assign", "Assign a Job Order", (), JoAssignInput, JoWriteOutput, "coordinator", "The Person must differ from the current assignee.", jo_write),
    Verb("jo", "reassign", "Reassign a Job Order", (), JoReassignInput, JoWriteOutput, "coordinator", "A reason is required.", jo_write),
    Verb("jo", "note", "Add a Job Order note", (), JoNoteInput, JoWriteOutput, "coordinator", "Notes are append-only.", jo_write),
    Verb("jo", "submit", "Submit a Job Order", (), JoSubmitInput, JoWriteOutput, "coordinator", "Apply the JO transition rules.", jo_write),
    Verb("jo", "close", "Close a Job Order", (), JoCloseInput, JoWriteOutput, "coordinator", "Apply the JO transition rules.", jo_write),
    Verb("jo", "block", "Block a Job Order", (), JoBlockInput, JoWriteOutput, "coordinator", "Apply the JO transition rules.", jo_write),
    Verb("jo", "unblock", "Unblock a Job Order", (), JoUnblockInput, JoWriteOutput, "coordinator", "Apply the JO transition rules.", jo_write),
    Verb("jo", "delay", "Delay a Job Order", (), JoDelayInput, JoWriteOutput, "coordinator", "Apply the JO transition rules.", jo_write),
    Verb("jo", "cancel", "Cancel a Job Order", (), JoCancelInput, JoWriteOutput, "coordinator", "Apply the JO transition rules.", jo_write),
    Verb("jo", "format", "Format a Job Order", (), JoFormatInput, JoFormatOutput, "reader", "Format a draft.", None),
    Verb("person", "find", "Find a Person", (), PersonFindInput, PersonFindOutput, "reader", "Find a Contact.", None, read_only=True),
    Verb("project", "resolve", "Resolve a Project", (), ProjectResolveInput, ProjectResolveOutput, "reader", "Resolve a Project.", None, read_only=True),
    Verb("camera", "get", "Get Cameras", (), CameraGetInput, CameraGetOutput, "reader", "Read Cameras.", None, read_only=True),
    Verb("camera", "enrich", "Enrich a Camera", (), CameraEnrichInput, CameraEnrichOutput, "operator", "Update a Camera.", None),
    Verb("departments", "", "Print the studio Departments, read live, in display order, with option IDs", (),
         DepartmentsInput, DepartmentsOutput, "reader", "Notion Department options are the truth; use studio display order.", departments, read_only=True),
)


def invoke(entry: Verb, inputs: BaseModel, *, actor: str, people: People,
           open_notion: Callable[..., Any],
           legacy: Callable[[], int] | None = None, frameio: Any = None,
           notion_source: Any = None) -> Outcome:
    """Authorize an authenticated Person before invoking a registered verb."""
    if isinstance(inputs, ConcernNoteInput) and inputs.from_reply is not None:
        person = people.get(actor)
        if not person or not person['active']:
            return Refused(rule='person', reason='An active replier Person is required')
    refusal = (None if entry.noun in ('approval', 'package', 'abang') and entry.role == 'person' and (person := people.get(actor)) and person['active'] else
               Refused(rule='person', reason='An active Person is required') if entry.noun in ('approval', 'package', 'abang') and entry.role == 'person' else
               people.authorize(actor, 'reader' if isinstance(inputs, ConcernNoteInput) and inputs.from_reply is not None else entry.role))
    if refusal:
        return refusal
    if entry.noun == 'person' and entry.verb in ('grant', 'revoke'):
        assert isinstance(inputs, PersonPermissionInput)
        return person_permission(people, entry.verb, inputs, actor=actor)
    if entry.handler is None:
        return Done(result=legacy()) if legacy is not None else Refused(rule='verb', reason='Verb has no registered handler')
    token = _actor.set(actor)
    registry_token = _registry.set(people.registry)
    frameio_token = _frameio.set(frameio)
    notion_token = _notion_source.set(notion_source)
    try:
        outcome = entry.handler(inputs, open_notion)
        if entry.noun in ('drive', 'movement', 'swing'):
            from t6.drives.practice import practice_mode
            practice = practice_mode(people.registry)
            if isinstance(outcome, Done) and isinstance(outcome.result, dict):
                outcome = outcome.model_copy(update={'result': {**outcome.result, 'practice': practice}})
            elif isinstance(outcome, Preview) and isinstance(outcome.plan, dict):
                outcome = outcome.model_copy(update={'plan': {**outcome.plan, 'practice': practice}})
        return outcome
    finally:
        _actor.reset(token)
        _registry.reset(registry_token)
        _frameio.reset(frameio_token)
        _notion_source.reset(notion_token)


def catalogue() -> list[dict[str, Any]]:
    entries = [{"noun": v.noun, "verb": v.verb, "description": v.description, "aliases": list(v.aliases),
                "role": v.role, "read_only": v.read_only, "rule": v.rule, "local_only": v.local_only, "input_schema": v.input_model.model_json_schema(),
                "output_schema": v.output_model.model_json_schema()} for v in VERBS]
    entries.append({'noun': 'admin deliverable', 'verb': 'migrate', 'description': 'Migrate Notion Deliverables',
                    'aliases': [], 'role': 'admin', 'read_only': False, 'rule': 'Preview before applying.', 'local_only': True,
                    'input_schema': DeliverableMigrateInput.model_json_schema(), 'output_schema': DeliverableMigrateOutput.model_json_schema()})
    entries.append({'noun': 'admin bay', 'verb': 'migrate', 'description': 'Migrate Notion Bays',
                    'aliases': [], 'role': 'admin', 'read_only': False, 'rule': 'Preview before applying.', 'local_only': True,
                    'input_schema': DeliverableMigrateInput.model_json_schema(), 'output_schema': DeliverableMigrateOutput.model_json_schema()})
    entries.append({'noun': 'admin media', 'verb': 'migrate', 'description': 'Migrate Notion External Links',
                    'aliases': [], 'role': 'admin', 'read_only': False, 'rule': 'Preview before applying.', 'local_only': False,
                    'input_schema': MediaMigrateInput.model_json_schema(), 'output_schema': MediaMigrateOutput.model_json_schema()})
    return entries



def _boolean(value: str) -> bool:
    if value.lower() in {"true", "1"}:
        return True
    if value.lower() in {"false", "0"}:
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def _json_list_file(value: str) -> Any:
    """A command-line argument naming a JSON file whose content is a list of objects (a Drive scan manifest)."""
    return json.loads(Path(value).read_text(encoding='utf-8'))


def _argument_type(annotation: Any) -> tuple[Any, bool]:
    origin = get_origin(annotation)
    if origin is UnionType or str(origin) == "typing.Union":
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(args) != 1:
            raise TypeError(f"unsupported CLI field type: {annotation}")
        return _argument_type(args[0])
    if origin is Literal:
        return str, False
    if origin is list:
        item, nested = _argument_type(get_args(annotation)[0])
        if nested:
            raise TypeError(f"unsupported CLI field type: {annotation}")
        return item, True
    if annotation is AwareDatetime:
        return str, False
    if annotation is bool:
        return _boolean, False
    if annotation in (str, int, float, Path):
        return annotation, False
    raise TypeError(f"unsupported CLI field type: {annotation}")


def add_parsers(commands: Any) -> None:
    nouns: dict[str, Any] = {}
    for entry in VERBS:
        if entry.handler is None:
            continue
        if entry.noun not in nouns:
            if entry.noun == "version":
                parser = commands.choices["version"]
                nouns[entry.noun] = parser.add_subparsers(dest="version_verb")
            elif entry.noun == "person":
                parser = commands.choices["contacts"]
                nouns[entry.noun] = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
            elif entry.noun == "jo":
                parser = commands.choices["jo"]
                nouns[entry.noun] = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
            elif entry.noun == "project":
                parser = commands.choices["projects"]
                nouns[entry.noun] = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
            else:
                parser = commands.add_parser(entry.noun, help=("[verb] " if not entry.verb else "") + entry.description)
                nouns[entry.noun] = parser.add_subparsers(dest=f"{entry.noun}_verb", required=True) if entry.verb else parser
        parser = nouns[entry.noun]
        if entry.noun == 'transcript':
            parser = parser.add_parser(entry.verb, help=entry.description)
            if entry.verb == 'create':
                parser.add_argument('media')
                for name in ('deliverable', 'manifest', 'language', 'start-timecode', 'confirm'):
                    parser.add_argument('--' + name, default='fil' if name == 'language' else None)
                parser.add_argument('--formats', nargs='+', default=['srt', 'docx'])
                parser.add_argument('--no-diarize', action='store_true')
                parser.add_argument('--max-hours', type=float)
            else:
                parser.add_argument('transcript')
                if entry.verb == 'speakers':
                    parser.add_argument('csv')
                if entry.verb in ('speakers', 'translate'):
                    parser.add_argument('--confirm')
            if entry.verb != 'get':
                parser.add_argument('--idempotency-key')
            parser.add_argument('--json', action='store_true')
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.handler is approval_write and entry.verb == 'request':
            for alias in entry.aliases:
                name = alias.split(' ', 1)[1]
                gate = name.removeprefix('request-')
                alias_parser = parser.add_parser(name, help='Request a ' + gate + ' Approval')
                alias_parser.add_argument('target')
                alias_parser.add_argument('--version')
                alias_parser.add_argument('--tapper-rule', type=lambda value: json.loads(value) if value.startswith('{') else value)
                alias_parser.add_argument('--idempotency-key')
                alias_parser.add_argument('--confirm')
                alias_parser.set_defaults(registered_verb=entry, gate=gate)
        if entry.handler in (drive_mapping_propose, drive_mapping_apply):
            if 'drive mapping' not in nouns:
                nouns['drive mapping'] = parser.add_parser('mapping', help='Sheet Project mappings').add_subparsers(dest='drive_mapping_verb', required=True)
            parser = nouns['drive mapping'].add_parser(entry.verb.split()[1], help=entry.description)
            parser.add_argument('--file', required=True)
            if entry.handler is drive_mapping_propose:
                parser.add_argument('--workbook', required=True)
                parser.add_argument('--tracker')
                parser.add_argument('--data-sources', type=Path, required=True)
            else:
                parser.add_argument('--idempotency-key')
                parser.add_argument('--confirm')
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.handler is drive_write:
            parser = parser.add_parser(entry.verb, help=entry.description)
            if entry.verb == 'release':
                parser.add_argument('drives', nargs='+')
                parser.add_argument('--project', required=True)
                parser.add_argument('--party', required=True)
                parser.add_argument('--party-kind', choices=('contact', 'studio'), required=True)
                parser.add_argument('--delivery')
            else:
                parser.add_argument('drive')
                parser.add_argument('project')
            if entry.verb in ('receive', 'return'):
                parser.add_argument('--party')
                parser.add_argument('--party-kind', choices=('contact', 'studio'))
            if entry.verb == 'retire':
                parser.add_argument('--reason', required=True)
            if entry.verb == 'assign':
                parser.add_argument('--label', required=True)
                parser.add_argument('--role', required=True, choices=DRIVE_ROLES)
                parser.add_argument('--start')
            if entry.verb == 'unassign':
                parser.add_argument('--label')
                parser.add_argument('--role', choices=DRIVE_ROLES)
                parser.add_argument('--assignment', type=int)
            if entry.verb in ('assign', 'unassign'):
                parser.add_argument('--end')
            else:
                parser.add_argument('--on')
            parser.add_argument('--idempotency-key')
            parser.add_argument('--confirm')
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.handler in (swing_write, swing_overdue):
            parser = parser.add_parser(entry.verb, help=entry.description)
            if entry.verb == 'assign':
                parser.add_argument('drive')
                parser.add_argument('target')
                parser.add_argument('--project')
                parser.add_argument('--label')
                parser.add_argument('--start')
                parser.add_argument('--return-date')
            elif entry.handler is swing_write:
                parser.add_argument('drive')
                parser.add_argument('--project')
                parser.add_argument('--person')
                parser.add_argument('--assignment', type=int)
                parser.add_argument('--end')
            if entry.handler is swing_write:
                parser.add_argument('--idempotency-key')
                parser.add_argument('--confirm')
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.handler in (frameio_project_link, frameio_project_unlink):
            if 'frameio project' not in nouns:
                nouns['frameio project'] = parser.add_parser('project', help='Frame.io projects').add_subparsers(dest='frameio_project_verb', required=True)
            parser = nouns['frameio project'].add_parser(entry.verb.split(' ')[1])
            parser.add_argument('project')
            parser.add_argument('frameio_project')
            parser.add_argument('--idempotency-key')
            parser.add_argument('--confirm')
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.handler in (media_write, media_list):
            parser = parser.add_parser(entry.verb, help=entry.description)
            if entry.verb == 'link':
                for name in ('owner', 'kind', 'ref'):
                    parser.add_argument(name)
                parser.add_argument('--role', required=True)
            elif entry.verb == 'unlink':
                parser.add_argument('media')
            else:
                parser.add_argument('owner')
            if entry.handler is media_write:
                parser.add_argument('--idempotency-key')
                parser.add_argument('--confirm')
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.handler in (timeline_write, timeline_restamp, timeline_list):
            parser = parser.add_parser(entry.verb, help=entry.description)
            parser.add_argument('project')
            if entry.handler is not timeline_list:
                parser.add_argument('timeline')
                parser.add_argument('--stamp', required=entry.handler is timeline_restamp)
                parser.add_argument('--idempotency-key')
                parser.add_argument('--confirm')
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.handler in (booking_write, booking_read):
            parser = parser.add_parser(entry.verb, help=entry.description)
            if entry.verb == 'book':
                for name in ('bay', 'person', 'project'):
                    parser.add_argument(name)
                parser.add_argument('--start', required=True)
                parser.add_argument('--end')
                parser.add_argument('--shift', choices=('day', 'night'))
            elif entry.verb != 'list':
                parser.add_argument('booking')
                if entry.verb == 'move':
                    parser.add_argument('--bay')
                    parser.add_argument('--start', required=True)
                if entry.verb in ('move', 'extend'):
                    parser.add_argument('--end', required=True)
            if entry.verb in ('book', 'move', 'extend'):
                parser.add_argument('--force', action='store_true')
            if entry.handler is booking_write:
                parser.add_argument('--idempotency-key')
                parser.add_argument('--confirm')
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.handler is version_write:
            parser = parser.add_parser(entry.verb, help=entry.description)
            if entry.verb == 'add':
                parser.add_argument('deliverable')
                parser.add_argument('--frameio-file', required=True)
                parser.add_argument('--note')
                parser.add_argument('--timeline')
            else:
                parser.add_argument('version')
            parser.add_argument('--idempotency-key')
            parser.add_argument('--confirm')
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.handler is milestone_record:
            milestone_parser = parser.add_parser('milestone', help='Deliverable milestones')
            parser = milestone_parser.add_subparsers(dest='milestone_verb', required=True).add_parser('record')
            parser.add_argument('deliverable')
            parser.add_argument('milestone')
            parser.add_argument('--on')
            parser.add_argument('--idempotency-key')
            parser.add_argument('--confirm')
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.verb:
            aliases = [alias.split(' ', 1)[1] for alias in entry.aliases] if entry.handler is approval_write and entry.verb != 'request' else []
            if entry.handler is drive_capture:
                parser = parser.add_parser('capture', help=entry.description)
                nouns['drive capture'] = parser.add_subparsers(dest='drive_capture_verb')
            elif entry.handler in (drive_capture_undo, drive_capture_result):
                parser = nouns['drive capture'].add_parser(entry.verb.split()[1], help=entry.description)
            else:
                parser = parser.add_parser(entry.verb, aliases=aliases, help="[verb] " + entry.description)
        if entry.handler is abang_write:
            parser.add_argument('person' if entry.verb == 'request' else 'candidate' if entry.verb == 'accept' else 'request')
            if entry.verb == 'accept':
                parser.add_argument('decision', choices=('yes', 'no'))
            if entry.verb == 'request':
                parser.add_argument('--destination', required=True)
                parser.add_argument('--note')
            parser.add_argument('--idempotency-key')
            parser.add_argument('--confirm')
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.handler is approval_write:
            if entry.verb == 'request':
                parser.add_argument('gate')
                parser.add_argument('target')
                parser.add_argument('--version')
                parser.add_argument('--tapper-rule', type=lambda value: json.loads(value) if value.startswith('{') else value)
            else:
                parser.add_argument('approval')
                if entry.verb == 'deny':
                    parser.add_argument('--reason', required=True)
            parser.add_argument('--idempotency-key')
            parser.add_argument('--confirm')
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.handler in (package_write, package_get, package_list):
            if entry.verb == 'prepare':
                parser.add_argument('deliverable')
                parser.add_argument('kind')
                for name in ('to', 'recipient', 'data-sources'):
                    parser.add_argument('--' + name, required=True, type=Path if name == 'data-sources' else str)
                for name in ('client-spec', 'note'):
                    parser.add_argument('--' + name)
            elif entry.verb == 'list':
                parser.add_argument('--missing', action='store_true')
            else:
                parser.add_argument('package')
                if entry.verb == 'check':
                    parser.add_argument('item')
                if entry.verb == 'reject':
                    parser.add_argument('--reason', default='')
            if entry.handler is package_write:
                parser.add_argument('--idempotency-key')
                parser.add_argument('--confirm')
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.handler is deliverable_schedule:
            parser.add_argument('deliverable')
            parser.add_argument('--reason', required=True)
            if entry.verb == 'delay':
                parser.add_argument('--field', choices=('due', 'air'), required=True)
                parser.add_argument('--date', required=True)
            parser.add_argument('--idempotency-key')
            parser.add_argument('--confirm')
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.handler is deliverable_deliver:
            parser.add_argument('deliverable')
            parser.add_argument('--carrier', required=True)
            parser.add_argument('--ref')
            parser.add_argument('--idempotency-key')
            parser.add_argument('--confirm')
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.handler is deliverable_rebind:
            parser.add_argument('deliverable_id')
            parser.add_argument('page_id')
            parser.add_argument('--idempotency-key')
            parser.add_argument('--confirm')
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.handler is deliverable_edit:
            parser.add_argument('deliverable')
            for name in ('code', 'episode', 'due', 'air', 'idempotency-key', 'confirm'):
                parser.add_argument('--' + name)
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.handler is deliverable_define:
            parser.add_argument('project')
            parser.add_argument('code')
            for name in ('episode', 'due', 'air', 'template', 'idempotency-key', 'confirm'):
                parser.add_argument('--' + name)
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.handler is concern_for_message:
            parser.add_argument('chat')
            parser.add_argument('message_id')
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.handler is jo_write and entry.verb == "raise":
            parser.add_argument("--file", type=Path)
            parser.add_argument("--data-sources", type=Path)
            parser.add_argument("--no-project", type=_boolean, default=False)
            parser.add_argument("--idempotency-key")
            parser.add_argument("--confirm")
            parser.set_defaults(registered_verb=entry, fields=None, message=None)
            continue
        if entry.handler is jo_write and entry.verb != "raise":
            parser.add_argument("jo")
            if entry.verb == "note":
                parser.add_argument("text")
            elif entry.verb in ("assign", "reassign"):
                parser.add_argument("person")
            if entry.verb in ("reassign", "block", "delay", "cancel"):
                parser.add_argument("--reason", required=True)
            if entry.verb == "delay":
                parser.add_argument("--until", required=True)
            parser.add_argument("--idempotency-key")
            parser.add_argument("--confirm")
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.handler is concern_observe:
            parser.add_argument("--file", type=Path, required=True, help="JSON object with run_date, findings and optionally unavailable")
            parser.add_argument("--dry-run", action="store_true")
            parser.add_argument("--idempotency-key", required=True)
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.handler in (llm_decide_api_only, llm_filter_api_only):
            for name, field in entry.input_model.model_fields.items():
                parser.add_argument("--" + name.replace("_", "-"), type=json.loads if name in ("state", "criteria", "candidates") else str,
                                    required=field.is_required(), default=field.get_default())
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.handler is llm_api_only:
            for name, field in entry.input_model.model_fields.items():
                parser.add_argument("--" + name.replace("_", "-"), type=json.loads if name == "output_schema" else str,
                                    required=field.is_required(), default=field.get_default())
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.handler in (project_link_chat, project_unlink_chat):
            parser.add_argument('project')
            parser.add_argument('--chat', type=int, required=True)
            parser.add_argument('--topic', type=int)
            parser.add_argument('--idempotency-key')
            parser.add_argument('--confirm')
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.handler is project_start:
            parser.add_argument('project')
            for name, label in (('deliverables', 'deliverable'), ('media', 'media'), ('bookings', 'booking')):
                parser.add_argument('--' + label, dest=name, action='append', type=json.loads, default=[], metavar='JSON',
                                    help=f'One {label} as a JSON object; repeat for more')
            parser.add_argument('--job', type=int, help='Read the Preview a plan job produced')
            parser.add_argument('--idempotency-key')
            parser.add_argument('--confirm')
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.handler in (project_hold, project_resume, project_archive):
            parser.add_argument("project")
            if entry.handler is project_hold:
                parser.add_argument("--reason", required=True)
            parser.add_argument("--idempotency-key")
            parser.add_argument("--confirm")
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.handler is project_activity:
            parser.add_argument("project")
            parser.add_argument("--since", help="ISO 8601 date or timestamp (UTC if no offset)")
            parser.add_argument("--limit", type=int)
            parser.add_argument("--json", action="store_true", help="Print JSON")
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.input_model is PersonProjectRoleInput:
            for name in ('person_id', 'project', 'role'):
                parser.add_argument(name)
            parser.add_argument('--idempotency-key')
            parser.add_argument('--confirm')
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.input_model in (MeetingNotesCreateInput, MeetingNotesAppendInput):
            if entry.input_model is MeetingNotesCreateInput:
                parser.add_argument('--title', required=True)
                parser.add_argument('--meeting-date', required=True, metavar='YYYY-MM-DD')
                parser.add_argument('--project-ids', nargs='+', default=[])
                parser.add_argument('--attendee-ids', nargs='+', default=[])
            else:
                parser.add_argument('--note', required=True)
            parser.add_argument('--block', dest='blocks', action='append', type=json.loads, default=[], metavar='JSON',
                                help='One body block as a JSON object (kind, text, spans, checked, children); repeat for more')
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.input_model is MeetingNotesMarkSentInput:
            parser.add_argument('--note', required=True)
            parser.add_argument('--at', type=datetime.fromisoformat, metavar='ISO', help='When it was sent, with a UTC offset (default: now, Manila)')
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.noun == 'bay' and entry.verb.startswith('scan '):
            if 'bay scan' not in nouns:
                nouns['bay scan'] = nouns['bay'].add_parser('scan', help='Bay scan reports').add_subparsers(dest='bay_scan_verb', required=True)
            parser = nouns['bay scan'].add_parser(entry.verb.split(' ')[1], help=entry.description)
            if entry.input_model is BayScanReportInput:
                parser.add_argument('--report', type=json.loads, required=True, metavar='JSON', help='The bay scan report as a JSON object, as the scan script prints it')
            else:
                parser.add_argument('--bay')
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.input_model is BayScanReportInput:
            parser.add_argument('--report', type=json.loads, required=True, metavar='JSON', help='The bay scan report as a JSON object, as the scan script prints it')
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.input_model in (MachineSyncInput, MachineEditInput):
            parser.add_argument('--machine', dest='machines', action='append', type=json.loads, required=True, metavar='JSON',
                                help='One Machine as a JSON object (its facts, or names and values for an edit); repeat for more')
            parser.add_argument('--confirm')
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.input_model is PersonShiftInput:
            parser.add_argument("person_id")
            for name in ("pattern", "start", "end"):
                parser.add_argument("--" + name, required=True)
            parser.add_argument("--idempotency-key")
            parser.add_argument("--confirm")
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.verb in ('grant', 'revoke') and entry.noun == 'person':
            parser.add_argument('person_id')
            parser.add_argument('permission')
            parser.add_argument('--idempotency-key')
            parser.add_argument('--confirm')
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.handler is person_get:
            parser.add_argument("--person-id", required=True)
            parser.add_argument("--data-sources", type=Path)
            parser.set_defaults(registered_verb=entry)
            continue
        if entry.handler is departments:
            parser.add_argument("--data-sources", type=Path)
            parser.add_argument("--include-misc", action="store_true")
            parser.set_defaults(registered_verb=entry)
            continue
        if (entry.noun, entry.verb) == ("project", "flags"):
            parser.add_argument("--project")
            parser.add_argument("--projects", type=json.loads, default=[], metavar="JSON",
                                help='Several Projects in place of --project, like [{"project": "t6p_...", "stated": {}}]')
            parser.add_argument("--as-of")
            parser.add_argument("--lead-warning-days", type=int, default=30)
            parser.add_argument("--stated", type=json.loads, default={}, metavar="JSON",
                                help='Dates from the PM Notes for blank properties, like {"playdate": "2026-11-20", "archival_deadline": null}')
            parser.set_defaults(registered_verb=entry)
            continue
        for name, field in entry.input_model.model_fields.items():
            if field.annotation == list[dict[str, Any]]:  # too big for flags: the argument names a JSON file
                parser.add_argument("--" + name.replace("_", "-"), type=_json_list_file, required=field.is_required(), metavar="FILE",
                                    help="Path of a JSON file holding the list of objects")
                continue
            arg_type, is_list = _argument_type(field.annotation)
            options: dict[str, Any] = {"type": arg_type, "required": field.is_required(),
                                       "default": None if field.is_required() else field.get_default(call_default_factory=True)}
            if is_list:
                options["nargs"] = "+"
            parser.add_argument("--" + name.replace("_", "-"), **options)
        parser.set_defaults(registered_verb=entry)
    commands_parser = commands.add_parser("commands", help="List registered verbs")
    commands_parser.add_argument("--json", action="store_true", required=True, help="Print the verb catalogue as JSON")


def run(args: argparse.Namespace, open_notion: Callable[..., Any]) -> int:
    entry: Verb = args.registered_verb
    if entry.noun == 'transcript' and entry.verb == 'speakers':
        from pathlib import Path
        args.csv = Path(args.csv).read_text(encoding='utf-8-sig')
    inputs = (ConcernObserveInput.model_validate({**json.loads(args.file.read_text(encoding='utf-8')), 'dry_run': args.dry_run, 'idempotency_key': args.idempotency_key}) if entry.handler is concern_observe else
              LegacyPersonGetInput(person_id=args.person_id, data_sources=args.data_sources) if entry.handler is person_get and args.data_sources else
              LegacyDepartmentsInput(data_sources=args.data_sources, include_misc=args.include_misc) if entry.handler is departments and args.data_sources else
              entry.input_model.model_validate({name: getattr(args, name) for name in entry.input_model.model_fields}))
    assert entry.handler is not None
    from t6.client import APIError, Client, Unavailable, cli_timeout
    try:
        outcome = Client(timeout=cli_timeout(entry.noun, entry.verb)).call(entry.noun, entry.verb, inputs)
    except APIError as error:
        outcome = Refused(rule="api", reason=str(error))
    if isinstance(outcome, Unavailable):
        print(json.dumps({"outcome": "unavailable", "reason": outcome.reason,
                          "idempotency_key": outcome.idempotency_key}, ensure_ascii=False, indent=2))
        return 1
    code, payload = outcome_json(outcome)
    if entry.noun == "departments" and not entry.verb:
        if isinstance(outcome, Done):
            payload = outcome.result.model_dump(mode="json") if isinstance(outcome.result, BaseModel) else outcome.result
        elif isinstance(outcome, Refused):
            from t6.cli import _fail
            return _fail(outcome.reason)
    if entry.handler is project_activity and not args.json and isinstance(outcome, Done):
        for item in outcome.result["entries"]:
            when = datetime.fromtimestamp(item["at"], timezone.utc).isoformat()
            print(f"{when}  {item['actor']}  {item['summary']}")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code
