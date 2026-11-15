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
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal, get_args, get_origin
from types import UnionType

from pydantic import BaseModel, Field, ConfigDict
from t6.domain import Project, JobOrder, Deliverable, MeetingNote, Contact, Camera, ExternalRef
from t6.llm.api import Done as Done, LLMRunInput as LLMRunInput, LLMRunOutput as LLMRunOutput

if TYPE_CHECKING:
    from t6.person import People


_actor: ContextVar[str] = ContextVar("verb_actor", default="")
_registry: ContextVar[Any] = ContextVar("verb_registry", default=None)


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


class Department(BaseModel):
    id: str
    name: str


class DepartmentsOutput(BaseModel):
    departments: list[Department]


def departments(inputs: LegacyDepartmentsInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.cli import TOKEN_VARIABLE, _read_data_sources

    try:
        from t6.notion import NotionError
    except ImportError:
        return Refused(rule="notion", reason="departments needs the notion extra: pip install 't6[notion]'")
    token = os.environ.get(TOKEN_VARIABLE)
    if not token:
        return Refused(rule="credentials", reason=f"set {TOKEN_VARIABLE} to a Notion integration token")
    try:
        sources = _read_data_sources(inputs.data_sources)
        if "job_orders" not in sources:
            return Refused(rule="data_sources", reason="--data-sources has no 'job_orders' entry")
        from t6.notion.departments import get_departments

        with open_notion(token) as client:
            result = DepartmentsOutput(departments=[Department(id=d.option_id, name=d.name) for d in get_departments(client, sources["job_orders"])])
        return Done(result=result)
    except (OSError, ValueError, ImportError, NotionError) as error:
        return Refused(rule="read", reason=str(error))


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
    kind: Literal['drive', 'nas-path', 'frameio-project', 'frameio-folder', 'resolve-project', 'timeline', 'instagantt-project']
    ref: str
    role: Literal['master', 'backup', 'review', 'working', 'frameio-project']


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
    deliverable_ids: list[str] = Field(default_factory=list)
    media_ids: list[str] = Field(default_factory=list)
    booking_ids: list[str] = Field(default_factory=list)
    manual_steps: list[str] = Field(default_factory=list)


def project_start(inputs: ProjectStartInput, open_notion: Callable[..., Any]) -> Outcome:
    """Queue the plan job, read its Preview with `job`, or apply the t6 steps with `confirm`; the rules are in `t6.project_start`."""
    from t6.project_start import ProjectStartStore
    if not os.environ.get('T6_PREVIEW_SIGNING_KEY'):
        return Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
    people = _people()
    store = ProjectStartStore(people.registry, people.tokens)
    values = inputs.model_dump(exclude={'job', 'idempotency_key', 'confirm'})
    if inputs.job is not None:
        return store.job_result(inputs.job, actor=_actor.get())
    if inputs.confirm:
        return store.confirm(values, actor=_actor.get(), idempotency_key=inputs.idempotency_key, token=inputs.confirm)
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
    kind: str
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
    from t6.cli import TOKEN_VARIABLE
    from t6.notion.contacts import Contacts
    from uuid import UUID
    try:
        person = _people().get(inputs.person_id)
        if person is None:
            return Refused(rule="person", reason="Person not found")
        token = os.environ.get(TOKEN_VARIABLE)
        if not token:
            return Refused(rule="credentials", reason=f"set {TOKEN_VARIABLE}")
        from t6.cli import _read_data_sources
        with open_notion(token) as client:
            page = next(b["page_id"] for b in person["bindings"] if b["active"])
            if len(page) == 32:
                try:
                    page = str(UUID(page))
                except ValueError:
                    pass
            person["display_name"] = Contacts(client, _read_data_sources(inputs.data_sources)["contacts"]).get(page).name
        return Done(result=person)
    except (ValueError, OSError, KeyError, StopIteration) as error:
        return Refused(rule="read", reason=str(error))


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


class CameraEnrichOutput(BaseModel):
    id: str
    name: str | None
    related_to_projects_cameras: list[str]
    tags: list[str]
    wiki: list[str]


class LegacyJoGetInput(BaseModel):
    ref: str
    data_sources: Path


class JoRaiseInput(BaseModel):
    file: Path
    data_sources: Path
    idempotency_key: str | None = None
    confirm: str | None = None


class JoWriteOutput(BaseModel):
    jo_id: str
    project_id: str
    action: str
    idempotency_key: str
    page_id: str | None = None
    sync_intent_id: int | None = None


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


def jo_write(inputs: JoRaiseInput | JoAssignInput | JoReassignInput | JoNoteInput | JoSubmitInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.cli import TOKEN_VARIABLE, _read_data_sources
    from t6.cli_jo import prepare_fields
    from t6.job_order import JobOrderStore
    from t6.notion import NotionError
    from t6.preview import PreviewTokens

    key = os.environ.get("T6_PREVIEW_SIGNING_KEY", "")
    if not key:
        return Refused(rule="config", reason="Set T6_PREVIEW_SIGNING_KEY")
    try:
        registry = _people().registry
        values: dict[str, Any]
        state_verbs = {JoSubmitInput: 'submit', JoCloseInput: 'close', JoUnblockInput: 'unblock',
                       JoBlockInput: 'block', JoCancelInput: 'cancel', JoDelayInput: 'delay'}
        if type(inputs) in state_verbs:
            verb = state_verbs[type(inputs)]
            values = inputs.model_dump(exclude={'idempotency_key', 'confirm'})
        elif isinstance(inputs, JoRaiseInput):
            token = os.environ.get(TOKEN_VARIABLE)
            if not token:
                return Refused(rule="credentials", reason=f"set {TOKEN_VARIABLE}")
            with open_notion(token) as client:
                fields = prepare_fields(client, _read_data_sources(inputs.data_sources)["job_orders"], inputs.file)
            verb, values = "raise", {"fields": fields}
        elif isinstance(inputs, JoReassignInput):
            verb, values = "reassign", {"jo": inputs.jo, "person": inputs.person, "reason": inputs.reason}
        elif isinstance(inputs, JoAssignInput):
            verb, values = "assign", {"jo": inputs.jo, "person": inputs.person}
        elif isinstance(inputs, JoNoteInput):
            verb, values = "note", {"jo": inputs.jo, "text": inputs.text}
        else:
            raise ValueError('Unknown Job Order input')
        return JobOrderStore(registry, PreviewTokens(key)).write(verb, values, actor=_actor.get(),
            idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)
    except (ValueError, OSError, KeyError, TypeError, NotionError) as error:
        return Refused(rule="input", reason=str(error))


class ConcernRaiseInput(BaseModel):
    project: str
    owner: str
    waiting_on: str
    anchor: str
    as_of: str
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
            if not token:
                return Refused(rule='credentials', reason=f'set {TOKEN_VARIABLE}')
            with open_notion(token) as client:
                store.specs = PackageSpecReader(client, _read_data_sources(inputs.data_sources)['package_specs'])
                return store.write(verb, values, actor=_actor.get(), idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)
        return store.write(verb, values, actor=_actor.get(), idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)
    except (OSError, ValueError, KeyError, NotionError) as error:
        return Refused(rule='spec', reason=str(error))


def package_list(inputs: PackageListInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.package import PackageStore
    return Done(result={'packages': PackageStore(_people().registry, _people().tokens).list(inputs.missing)})


def package_get(inputs: PackageGetInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.package import PackageStore
    record = PackageStore(_people().registry, _people().tokens).get(inputs.package)
    return Done(result={'package': record}) if record else Refused(rule='package', reason='Package not found')


class JoFormatInput(BaseModel):
    file: Path
    date: str | None = None


class LegacyPersonFindInput(BaseModel):
    mention: str
    data_sources: Path


class LegacyProjectResolveInput(BaseModel):
    text: str
    data_sources: Path
    registry_db: Path | None = None


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


class ReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DepartmentsInput(ReadInput):
    pass


class PersonGetInput(ReadInput):
    person_id: str


class PersonFindInput(ReadInput):
    mention: str


class PersonListInput(ReadInput):
    pass


class ProjectGetInput(ReadInput):
    project: str


class ProjectFlagsInput(ReadInput):
    project: str


class ProjectFlagsOutput(BaseModel):
    flags: list[dict[str, Any]]


class ProjectResolveInput(ReadInput):
    text: str


class JoGetInput(ReadInput):
    ref: str


class CameraGetInput(ReadInput):
    page_id: str | None = None


class StandingCcInput(ReadInput):
    pass


class StudioNoticesInput(ReadInput):
    pass


class StudioNoticesOutput(BaseModel):
    description: str


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


class ProjectListInput(ReadInput):
    rotation: Literal["in", "out", "unclassified"] | None = None
    status: str | None = None


class ProjectListOutput(BaseModel):
    projects: list[Project]


class JoListInput(ReadInput):
    project: str | None = None
    assignee: str | None = None
    status_group: Literal["not_started", "started", "cancelled", "active"] | None = None


class JoFindInput(ReadInput):
    telegram_key: str


class DeliverableDefineInput(BaseModel):
    project: str
    code: str
    template: str | None = None
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
        token = os.environ.get(TOKEN_VARIABLE)
        if not token:
            raise ValueError(f'set {TOKEN_VARIABLE} to read Project Type')
        with open_notion(token) as client:
            return ProjectsRow.from_page(client.retrieve_page(page)).project_type

    try:
        return DeliverableStore(_people().registry, _people().tokens, project_type).define(
            inputs.model_dump(exclude={'idempotency_key', 'confirm'}), actor=_actor.get(),
            idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)
    except (NotionError, OSError, ValueError) as error:
        return Refused(rule='project_type', reason=str(error))


class DeliverableListInput(ReadInput):
    project: str


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


@dataclass(frozen=True, slots=True, kw_only=True)
class ReadJobOrder(JobOrder):
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
    project: Project


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


def api_read(inputs: BaseModel, open_notion: Callable[..., Any]) -> Outcome:
    return Refused(rule="api", reason="Reads run on the t6 API")



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


class BookingMoveInput(BaseModel):
    booking: str
    start: str
    end: str
    bay: str | None = None
    force: bool = False
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
    kind: Literal['drive', 'nas-path', 'frameio-project', 'frameio-folder', 'resolve-project', 'timeline', 'instagantt-project']
    ref: str
    role: Literal['master', 'backup', 'review', 'working', 'frameio-project']
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
    stamp: str
    idempotency_key: str | None = None
    confirm: str | None = None


class TimelineWriteOutput(BaseModel):
    timeline_id: str
    project_id: str
    created: bool
    idempotency_key: str


class TimelineListInput(BaseModel):
    project: str


class TimelineListOutput(BaseModel):
    timelines: list[dict[str, Any]]


def timeline_write(inputs: TimelineLinkInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.timeline import TimelineStore
    if not os.environ.get('T6_PREVIEW_SIGNING_KEY'):
        return Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
    people = _people()
    return TimelineStore(people.registry, people.tokens).write(
        inputs.model_dump(exclude={'confirm', 'idempotency_key'}), actor=_actor.get(),
        idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)


def timeline_list(inputs: TimelineListInput, open_notion: Callable[..., Any]) -> Outcome:
    from t6.timeline import TimelineStore
    people = _people()
    return Done(result={'timelines': TimelineStore(people.registry, people.tokens).list(inputs.project)})


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
    role: Literal['master', 'backup', 'offline', 'swing', 'archive']
    start: str | None = None
    end: str | None = None
    idempotency_key: str | None = None
    confirm: str | None = None


class DriveUnassignInput(BaseModel):
    drive: str
    project: str
    label: str | None = None
    role: Literal['master', 'backup', 'offline', 'swing', 'archive'] | None = None
    assignment: int | None = None
    end: str | None = None
    idempotency_key: str | None = None
    confirm: str | None = None


class DriveWriteOutput(BaseModel):
    action: str
    drive_ids: list[str]
    project_id: str
    on: str | None = None
    shipment_id: str | None = None
    delivery_id: str | None = None
    label: str | None = None
    role: str | None = None
    assignment: int | None = None
    start: str | None = None
    end: str | None = None
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
    assignment: int | None = None
    end: str | None = None
    idempotency_key: str | None = None
    confirm: str | None = None


class SwingReturnInput(SwingUnassignInput):
    pass


class SwingWriteOutput(DriveWriteOutput):
    holder: str | None = None
    return_date: str | None = None


class SwingOverdueInput(BaseModel):
    pass


class SwingOverdueOutput(BaseModel):
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

VERBS = (
    Verb('transcript', 'create', 'Create a Transcript', (), TranscriptCreateInput, TranscriptCreateOutput, 'staff', 'Preview and confirm a transcription job.', api_read),
    Verb('transcript', 'speakers', 'Name Transcript speakers', (), TranscriptSpeakersInput, TranscriptCreateOutput, 'staff', 'Preview and confirm speaker names.', api_read),
    Verb('transcript', 'translate', 'Translate a Transcript', (), TranscriptTranslateInput, TranscriptCreateOutput, 'staff', 'Preview and confirm translation.', api_read),
    Verb('transcript', 'get', 'Get a Transcript', (), TranscriptGetInput, TranscriptGetOutput, 'reader', 'Read progress and summary.', transcript_get, read_only=True),
    Verb('drive', 'receive', 'Receive a Drive into custody', (), DriveReceiveInput, DriveWriteOutput, 'drives', 'Preview and confirm; appends a received event.', drive_write),
    Verb('drive', 'release', 'Release Drives in one shipment', (), DriveReleaseInput, DriveWriteOutput, 'drives', 'Preview and confirm; one released event per Drive, grouped by shipment.', drive_write),
    Verb('drive', 'return', 'Return a Drive to custody', (), DriveReturnInput, DriveWriteOutput, 'drives', 'Preview and confirm; appends a returned event.', drive_write),
    Verb('drive', 'retire', 'Retire a Drive', (), DriveRetireInput, DriveWriteOutput, 'drives', 'Needs a reason; a later receive or return brings the Drive back.', drive_write),
    Verb('drive', 'assign', 'Assign a Drive to a Project', (), DriveAssignInput, DriveWriteOutput, 'drives', 'Preview and confirm; adds an Assignment with label and role.', drive_write),
    Verb('drive', 'unassign', 'End a Drive Assignment', (), DriveUnassignInput, DriveWriteOutput, 'drives', 'Ends the open Assignment and keeps it in history; when several are open, pick by label, role or assignment number.', drive_write),
    Verb('swing', 'assign', 'Assign a swing Drive to a Person or Project', (), SwingAssignInput, SwingWriteOutput, 'drives', 'Preview and confirm; adds a swing Assignment, with an optional return date. A Person target also needs --project.', swing_write),
    Verb('swing', 'unassign', 'End a swing Assignment', (), SwingUnassignInput, SwingWriteOutput, 'drives', 'Ends the open swing Assignment and keeps it, with its return date, in history.', swing_write),
    Verb('swing', 'return', 'Return a swing Drive', (), SwingReturnInput, SwingWriteOutput, 'drives', 'Ends the open swing Assignment and clears its return date.', swing_write),
    Verb('swing', 'overdue', 'List swing Drives past their return date', (), SwingOverdueInput, SwingOverdueOutput, 'reader', 'Open swing Assignments due back before today (Asia/Manila).', swing_overdue, read_only=True),
    Verb('media', 'link', 'Link a Media location', (), MediaLinkInput, MediaWriteOutput, 'coordinator', 'Preview and confirm the link.', media_write),
    Verb('media', 'unlink', 'Unlink a Media location', (), MediaUnlinkInput, MediaWriteOutput, 'coordinator', 'Preview and confirm the unlink.', media_write),
    Verb('frameio', 'project link', 'Link a Frame.io project to a Project', (), FrameioProjectInput, MediaWriteOutput, 'staff', 'One Frame.io project belongs to one Project. Preview and confirm.', frameio_project_link),
    Verb('frameio', 'project unlink', 'Unlink a Frame.io project from a Project', (), FrameioProjectInput, MediaWriteOutput, 'staff', 'Moving is unlink then link. Preview and confirm.', frameio_project_unlink),
    Verb('timeline', 'link', 'Record a hand-written Timeline stamp', (), TimelineLinkInput, TimelineWriteOutput, 'staff', 'The stamp is written by hand in Resolve; one Timeline per stamp. Preview and confirm.', timeline_write),
    Verb('timeline', 'list', 'List Timelines of a Project', (), TimelineListInput, TimelineListOutput, 'reader', 'Read Timelines with their canonical copy.', timeline_list, read_only=True),
    Verb('media', 'list', 'List Media locations', (), MediaListInput, MediaListOutput, 'reader', 'Read Media locations.', media_list, read_only=True),
    Verb('bay', 'book', 'Book a Bay', (), BayBookInput, BookingOutput, 'bookings', 'Overlaps require force.', booking_write),
    Verb('booking', 'move', 'Move a Booking', (), BookingMoveInput, BookingOutput, 'bookings', 'Overlaps require force.', booking_write),
    Verb('booking', 'extend', 'Extend a Booking', (), BookingExtendInput, BookingOutput, 'bookings', 'Overlaps require force.', booking_write),
    Verb('booking', 'cancel', 'Cancel a Booking', (), BookingCancelInput, BookingOutput, 'bookings', 'Cancel an active Booking.', booking_write),
    Verb('booking', 'get', 'Get a Booking', (), BookingGetInput, BookingGetOutput, 'reader', 'Read a Booking.', booking_read, read_only=True),
    Verb('booking', 'list', 'List Bookings', (), BookingListInput, BookingListOutput, 'reader', 'Read Bookings.', booking_read, read_only=True),
    Verb("version", "get", "Get a Version", (), VersionGetInput, VersionFindOutput, "reader", "Read a Version and its Approvals.", api_read, read_only=True),
    Verb("version", "find", "Find a Version by Frame.io file", (), VersionFindInput, VersionFindOutput, "reader", "Resolve a review file to its Version.", api_read, read_only=True),
    Verb("version", "add", "Add a Version", (), VersionAddInput, VersionOutput, "coordinator", "Preview and confirm a Frame.io file.", version_write),
    Verb("version", "lock", "Lock a Version", (), VersionLockInput, VersionOutput, "coordinator", "Record picture lock.", version_write),
    Verb('approval', 'request', 'Request an Approval', tuple('approval request-' + gate for gate in ('vfx-committee', 'client-cut', 'grade', 'delivery-sign-off', 'drive-wipe', 'fleet-promotion', 'project-start')), ApprovalRequestInput, ApprovalOutput, 'person', 'An active Person may request an Approval.', approval_write),
    Verb('approval', 'grant', 'Grant an Approval', tuple('approval grant-' + gate for gate in ('vfx-committee', 'client-cut', 'grade', 'delivery-sign-off', 'drive-wipe', 'fleet-promotion', 'project-start')), ApprovalGrantInput, ApprovalOutput, 'person', 'The allowed-tapper rule applies.', approval_write),
    Verb('approval', 'deny', 'Deny an Approval', tuple('approval deny-' + gate for gate in ('vfx-committee', 'client-cut', 'grade', 'delivery-sign-off', 'drive-wipe', 'fleet-promotion', 'project-start')), ApprovalDenyInput, ApprovalOutput, 'person', 'The allowed-tapper rule and a reason are required.', approval_write),
    Verb("studio", "notices", "Read studio notices", (), StudioNoticesInput, StudioNoticesOutput, "reader", "Read Project source notices.", api_read, read_only=True),
    Verb("external-link", "find", "Find an External Link", (), ExternalLinkFindInput, ExternalLinkFindOutput, "reader", "Resolve an external reference.", api_read, read_only=True),
    Verb("project", "get", "Get a Project", (), ProjectGetInput, ProjectGetOutput, "reader", "Read a Project.", api_read, read_only=True),
    Verb("project", "flags", "Read Project flags", (), ProjectFlagsInput, ProjectFlagsOutput, "reader", "Recompute data gaps and date warnings.", api_read, read_only=True),
    Verb("person", "list", "List Contacts", (), PersonListInput, PersonListOutput, "reader", "Read Contacts.", api_read, read_only=True),
    Verb("standing-cc", "", "Get standing cc", (), StandingCcInput, StandingCcOutput, "reader", "Read configured standing cc.", None, read_only=True),
    Verb("jo", "list", "List Job Orders", (), JoListInput, JoListOutput, "reader", "Filter by Project, assignee or status group.", api_read, read_only=True),
    Verb("jo", "find", "Find Job Orders by Telegram Key", (), JoFindInput, JoListOutput, "reader", "Return all matching orders.", api_read, read_only=True),
    Verb("deliverable", "milestone record", "Record a Deliverable milestone", (), MilestoneRecordInput, MilestoneRecordOutput, "coordinator", "Milestones are append-only.", milestone_record),
    Verb("deliverable", "miss", "Record a missed Deliverable", (), DeliverableMissInput, DeliverableScheduleOutput, "coordinator", "A delivered Deliverable cannot be missed.", deliverable_schedule),
    Verb("deliverable", "delay", "Move a Deliverable date", (), DeliverableDelayInput, DeliverableScheduleOutput, "coordinator", "Date moves require a reason.", deliverable_schedule),
    Verb("deliverable", "deliver", "Deliver to the client", (), DeliverableDeliverInput, DeliverableDeliverOutput, "coordinator", "Blocking checklist items must be checked.", deliverable_deliver),
    Verb("deliverable", "define", "Define a Deliverable", (), DeliverableDefineInput, DeliverableDefineOutput, "operator", "Preview and confirm a Deliverable.", deliverable_define),
    Verb("deliverable", "rebind", "Bind a replacement Deliverables page", (), DeliverableRebindInput, DeliverableDefineOutput, "operator", "Retain the old page as a legacy binding.", deliverable_rebind),
    Verb("deliverable", "get", "Get a Deliverable", (), DeliverableGetInput, DeliverableGetOutput, "reader", "Read from t6.", api_read, read_only=True),
    Verb("deliverable", "list", "List Project Deliverables", (), DeliverableListInput, DeliverableListOutput, "reader", "Read from t6.", api_read, read_only=True),
    Verb("deliverable", "bangko", "List Deliverables ready ahead of air", (), FloorReadInput, FloorReadOutput, "reader", "Ready (kasado or delivered) at least the configured days before air; optional project.", floor_bangko, read_only=True),
    Verb("deliverable", "hand-to-mouth", "List Deliverables ready at the last moment", (), FloorReadInput, FloorReadOutput, "reader", "Ready within the configured days of air; optional project.", floor_hand_to_mouth, read_only=True),
    Verb("deliverable", "sabit", "List missed Deliverables", (), FloorReadInput, FloorReadOutput, "reader", "A recorded miss, not a delay; optional project.", floor_sabit, read_only=True),
    Verb("meeting-notes", "list", "List Project Meeting Notes", (), MeetingNotesListInput, MeetingNotesListOutput, "reader", "Read from Notion.", api_read, read_only=True),
    Verb("project", "list", "List Projects", (), ProjectListInput, ProjectListOutput, "reader", "Read Projects.", api_read, read_only=True),
    Verb("llm", "run", "Run an LLM task on the API", (), LLMRunInput, LLMRunOutput, "llm", "Server policy and masking apply.", llm_api_only),
    Verb("person", "add", "Register a Person", (), PersonAddInput, PersonWriteOutput, "operator", "A page belongs to one Person.", person_write),
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
    Verb("project", "start", "Start a Project: a previewed plan, then its t6 steps", (), ProjectStartInput, ProjectStartOutput, "operator", "A preview by job; confirm applies the Approval, Deliverables, Media locations and Bookings once. The manual steps are listed and recorded.", project_start),
    Verb("project", "archive", "Archive a Project", (), ProjectResumeInput, ProjectWriteOutput, "coordinator", "An archived Project cannot accept new work.", project_archive),
    Verb("project", "hold", "Hold a Project", (), ProjectHoldInput, ProjectWriteOutput,
         "operator", "A held Project cannot be held again.", project_hold),
    Verb("project", "resume", "Resume a held Project", (), ProjectResumeInput, ProjectWriteOutput,
         "operator", "Only a held Project can be resumed.", project_resume),
    Verb("project", "activity", "Show Project activity, newest first", (),
         ActivityInput, ActivityOutput, "reader", "Activity is append-only.", project_activity, read_only=True),
    Verb('package', 'list', 'List open Packages', (), PackageListInput, PackageListOutput, 'reader', 'Filter to Packages with missing items.', package_list, read_only=True),
    Verb('package', 'get', 'Get a Package', (), PackageGetInput, PackageGetOutput, 'reader', 'Read a Package and its checklist.', package_get, read_only=True),
    Verb('package', 'prepare', 'Prepare a Package', (), PackagePrepareInput, PackageOutput, 'person', 'Copy the Package Spec.', package_write),
    Verb('package', 'check', 'Check a Package item', (), PackageCheckInput, PackageOutput, 'person', 'Record who checked the item and when.', package_write),
    Verb('package', 'handoff', 'Hand off a Package', (), PackageHandoffInput, PackageOutput, 'person', 'Blocking items must be checked.', package_write),
    Verb('package', 'accept', 'Accept a Package', (), PackageAcceptInput, PackageOutput, 'person', 'The recipient accepts.', package_write),
    Verb('package', 'reject', 'Reject a Package', (), PackageRejectInput, PackageOutput, 'person', 'The recipient gives a reason.', package_write),
    Verb("concern", "for-message", "Find Concerns carried by a brief", (), ConcernForMessageInput, ConcernForMessageOutput, "reader", "Return Concern IDs for a brief message.", concern_for_message, read_only=True),
    Verb("concern", "record-message", "Record a Concern carried by a brief", (), ConcernRecordMessageInput, ConcernOutput, "coordinator", "Record a brief message reference without chat text.", concern_write),
    Verb("concern", "get", "Get a Concern", (), ConcernGetInput, ConcernGetOutput, "reader", "Read a Concern and its age.", concern_get, read_only=True),
    Verb("concern", "raise", "Raise a Concern", (), ConcernRaiseInput, ConcernOutput, "coordinator", "Record a line waiting on a party.", concern_write),
    Verb("concern", "note", "Add a Concern note", (), ConcernNoteInput, ConcernOutput, "coordinator", "Notes are append-only.", concern_write),
    Verb("concern", "resolve", "Resolve a Concern", (), ConcernResolveInput, ConcernOutput, "coordinator", "A person resolves the Concern.", concern_write),
    Verb("concern", "reopen", "Reopen a Concern", (), ConcernReopenInput, ConcernOutput, "coordinator", "Keep its original age.", concern_write),
    Verb("jo", "get", "Get a Job Order", (), JoGetInput, JoGetOutput, "reader", "Read a Job Order.", None, read_only=True),
    Verb("jo", "raise", "Raise a Job Order", ("jo create",), JoRaiseInput, JoWriteOutput, "coordinator", "Create a Job Order.", jo_write),
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
    Verb("person", "find", "Find a Person", ("contacts find",), PersonFindInput, PersonFindOutput, "reader", "Find a Contact.", None, read_only=True),
    Verb("project", "resolve", "Resolve a Project", ("projects resolve",), ProjectResolveInput, ProjectResolveOutput, "reader", "Resolve a Project.", None, read_only=True),
    Verb("camera", "get", "Get Cameras", ("cameras get",), CameraGetInput, CameraGetOutput, "reader", "Read Cameras.", None, read_only=True),
    Verb("camera", "enrich", "Enrich a Camera", ("cameras update",), CameraEnrichInput, CameraEnrichOutput, "operator", "Update a Camera.", None),
    Verb("departments", "", "Print the studio Departments, read live, in display order, with option IDs", (),
         DepartmentsInput, DepartmentsOutput, "reader", "Notion Department options are the truth; use studio display order.", departments, read_only=True),
)


def invoke(entry: Verb, inputs: BaseModel, *, actor: str, people: People,
           open_notion: Callable[..., Any],
           legacy: Callable[[], int] | None = None) -> Outcome:
    """Authorize an authenticated Person before invoking a registered verb."""
    if isinstance(inputs, ConcernNoteInput) and inputs.from_reply is not None:
        person = people.get(actor)
        if not person or not person['active']:
            return Refused(rule='person', reason='An active replier Person is required')
    refusal = (None if entry.noun in ('approval', 'package') and entry.role == 'person' and (person := people.get(actor)) and person['active'] else
               Refused(rule='person', reason='An active Person is required') if entry.noun in ('approval', 'package') and entry.role == 'person' else
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
    try:
        return entry.handler(inputs, open_notion)
    finally:
        _actor.reset(token)
        _registry.reset(registry_token)


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
                parser.add_argument('--role', required=True, choices=('master', 'backup', 'offline', 'swing', 'archive'))
                parser.add_argument('--start')
            if entry.verb == 'unassign':
                parser.add_argument('--label')
                parser.add_argument('--role', choices=('master', 'backup', 'offline', 'swing', 'archive'))
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
        if entry.handler in (timeline_write, timeline_list):
            parser = parser.add_parser(entry.verb, help=entry.description)
            parser.add_argument('project')
            if entry.handler is timeline_write:
                parser.add_argument('timeline')
                parser.add_argument('--stamp', required=True)
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
            parser = parser.add_parser(entry.verb, aliases=aliases, help="[verb] " + entry.description)
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
            parser.set_defaults(registered_verb=entry)
            continue
        for name, field in entry.input_model.model_fields.items():
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
    inputs = (LegacyPersonGetInput(person_id=args.person_id, data_sources=args.data_sources) if entry.handler is person_get and args.data_sources else
              LegacyDepartmentsInput(data_sources=args.data_sources) if entry.handler is departments and args.data_sources else
              entry.input_model.model_validate({name: getattr(args, name) for name in entry.input_model.model_fields}))
    assert entry.handler is not None
    from t6.client import APIError, Client, Unavailable
    try:
        outcome = Client().call(entry.noun, entry.verb, inputs)
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
