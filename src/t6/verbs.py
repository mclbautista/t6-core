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

from pydantic import BaseModel, Field, RootModel

if TYPE_CHECKING:
    from t6.person import People


_actor: ContextVar[str] = ContextVar("verb_actor", default="")


class Done(BaseModel):
    outcome: Literal["done"] = "done"
    result: Any


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


class DepartmentsInput(BaseModel):
    data_sources: Path


class Department(BaseModel):
    id: str
    name: str


class DepartmentsOutput(BaseModel):
    departments: list[Department]


def departments(inputs: DepartmentsInput, open_notion: Callable[..., Any]) -> Outcome:
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
        registry = _open_registry(Path(os.environ.get(REGISTRY_DB_VARIABLE, DEFAULT_REGISTRY_DB)))
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
    registry = _open_registry(Path(os.environ.get(REGISTRY_DB_VARIABLE, DEFAULT_REGISTRY_DB)))
    if registry is None:
        return Refused(rule="registry", reason="Project registry is unavailable")
    try:
        return ProjectWrites(registry, PreviewTokens(key)).run(
            verb, inputs.project, inputs.reason if isinstance(inputs, ProjectHoldInput) else None,
            actor=_actor.get(), idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)
    except (RegistryError, OSError) as error:
        return Refused(rule="registry", reason=str(error))


def project_hold(inputs: ProjectHoldInput, open_notion: Callable[..., Any]) -> Outcome:
    return _project_write(inputs, "hold")


def project_resume(inputs: ProjectResumeInput, open_notion: Callable[..., Any]) -> Outcome:
    return _project_write(inputs, "resume")

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


class PersonPermissionInput(BaseModel):
    person_id: str
    permission: str
    idempotency_key: str | None = None
    confirm: str | None = None


class PersonGetInput(BaseModel):
    person_id: str
    data_sources: Path


def _people() -> People:
    from t6.cli import DEFAULT_REGISTRY_DB, REGISTRY_DB_VARIABLE, _open_registry
    from t6.person import People
    from t6.preview import PreviewTokens
    registry = _open_registry(Path(os.environ.get(REGISTRY_DB_VARIABLE, DEFAULT_REGISTRY_DB)))
    if registry is None:
        raise ValueError("Registry is unavailable")
    return People(registry, PreviewTokens(os.environ.get("T6_PREVIEW_SIGNING_KEY", "read-only")))


def person_write(inputs: PersonAddInput | PersonTelegramInput | PersonRebindInput | PersonPermissionInput, open_notion: Callable[..., Any]) -> Outcome:
    if not os.environ.get("T6_PREVIEW_SIGNING_KEY"):
        return Refused(rule="config", reason="Set T6_PREVIEW_SIGNING_KEY")
    try:
        values = inputs.model_dump(exclude={"confirm", "idempotency_key"})
        return _people().write("add" if isinstance(inputs, PersonAddInput) else "rebind" if isinstance(inputs, PersonRebindInput) else "set-telegram", values,
                               actor=_actor.get(),
                               idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)
    except (ValueError, OSError) as error:
        return Refused(rule="input", reason=str(error))


def person_permission(people: People, verb: str, inputs: PersonPermissionInput, *, actor: str) -> Outcome:
    """Run a permission write as the authenticated Person."""
    return people.write(verb, inputs.model_dump(exclude={'confirm', 'idempotency_key'}),
                        actor=actor, idempotency_key=inputs.idempotency_key, confirm=inputs.confirm)


def person_get(inputs: PersonGetInput, open_notion: Callable[..., Any]) -> Outcome:
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


# Legacy CLI verbs retain their established arguments and JSON output while
# participating in discovery. Their handlers live in the CLI modules.
class LegacyOutput(RootModel[Any]):
    """The legacy commands retain their existing, command-specific JSON shapes."""


class JoGetInput(BaseModel):
    ref: str
    data_sources: Path


class JoRaiseInput(BaseModel):
    file: Path
    data_sources: Path
    apply: bool = False
    actor: str | None = None


class JoFormatInput(BaseModel):
    file: Path
    date: str | None = None


class PersonFindInput(BaseModel):
    mention: str
    data_sources: Path


class ProjectResolveInput(BaseModel):
    text: str
    data_sources: Path
    registry_db: Path | None = None


class CameraGetInput(BaseModel):
    data_sources: Path
    page_id: str | None = None


class CameraEnrichInput(BaseModel):
    page_id: str
    data_sources: Path
    json_file: Path | None = Field(default=None, alias="json")


VERBS = (
    Verb("person", "add", "Register a Person", (), PersonAddInput, LegacyOutput, "operator", "A page belongs to one Person.", person_write),
    Verb("person", "rebind", "Bind a replacement Contacts page", (), PersonRebindInput, LegacyOutput, "operator", "A page belongs to one Person.", person_write),
    Verb("person", "set-telegram", "Set a Person Telegram ID", (), PersonTelegramInput, LegacyOutput, "operator", "An active Person uniquely holds a Telegram ID.", person_write),
    Verb("person", "grant", "Grant a permission", (), PersonPermissionInput, LegacyOutput, "operator", "Only an operator or admin may grant permissions.", person_write),
    Verb("person", "revoke", "Revoke a permission", (), PersonPermissionInput, LegacyOutput, "operator", "Only an operator or admin may revoke permissions.", person_write),
    Verb("person", "get", "Get a Person", (), PersonGetInput, LegacyOutput, "reader", "Read a Person.", person_get),
    Verb("project", "hold", "Hold a Project", (), ProjectHoldInput, ProjectWriteOutput,
         "operator", "A held Project cannot be held again.", project_hold),
    Verb("project", "resume", "Resume a held Project", (), ProjectResumeInput, ProjectWriteOutput,
         "operator", "Only a held Project can be resumed.", project_resume),
    Verb("project", "activity", "Show Project activity, newest first", (),
         ActivityInput, ActivityOutput, "reader", "Activity is append-only.", project_activity),
    Verb("jo", "get", "Get a Job Order", (), JoGetInput, LegacyOutput, "reader", "Read a Job Order.", None),
    Verb("jo", "raise", "Raise a Job Order", ("jo create",), JoRaiseInput, LegacyOutput, "coordinator", "Create a Job Order.", None),
    Verb("jo", "format", "Format a Job Order", (), JoFormatInput, LegacyOutput, "reader", "Format a draft.", None),
    Verb("person", "find", "Find a Person", ("contacts find",), PersonFindInput, LegacyOutput, "reader", "Find a Contact.", None),
    Verb("project", "resolve", "Resolve a Project", ("projects resolve",), ProjectResolveInput, LegacyOutput, "reader", "Resolve a Project.", None),
    Verb("camera", "get", "Get Cameras", ("cameras get",), CameraGetInput, LegacyOutput, "reader", "Read Cameras.", None),
    Verb("camera", "enrich", "Enrich a Camera", ("cameras update",), CameraEnrichInput, LegacyOutput, "operator", "Update a Camera.", None),
    Verb("departments", "", "Print the studio Departments, read live, in display order, with option IDs", (),
         DepartmentsInput, DepartmentsOutput, "reader", "Notion Department options are the truth; use studio display order.", departments),
)


def invoke(entry: Verb, inputs: BaseModel, *, actor: str, people: People,
           open_notion: Callable[..., Any],
           legacy: Callable[[], int] | None = None) -> Outcome:
    """Authorize an authenticated Person before invoking a registered verb."""
    refusal = people.authorize(actor, entry.role)
    if refusal:
        return refusal
    if entry.noun == 'person' and entry.verb in ('grant', 'revoke'):
        assert isinstance(inputs, PersonPermissionInput)
        return person_permission(people, entry.verb, inputs, actor=actor)
    if entry.handler is None:
        return Done(result=legacy()) if legacy is not None else Refused(rule='verb', reason='Verb has no registered handler')
    token = _actor.set(actor)
    try:
        return entry.handler(inputs, open_notion)
    finally:
        _actor.reset(token)


def catalogue() -> list[dict[str, Any]]:
    return [{"noun": v.noun, "verb": v.verb, "description": v.description, "aliases": list(v.aliases),
             "role": v.role, "rule": v.rule, "input_schema": v.input_model.model_json_schema(),
             "output_schema": v.output_model.model_json_schema()} for v in VERBS]


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
            if entry.noun == "person":
                parser = commands.choices["contacts"]
                nouns[entry.noun] = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
            elif entry.noun == "project":
                parser = commands.choices["projects"]
                nouns[entry.noun] = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
            else:
                parser = commands.add_parser(entry.noun, help=("[verb] " if not entry.verb else "") + entry.description)
                nouns[entry.noun] = parser.add_subparsers(dest=f"{entry.noun}_verb", required=True) if entry.verb else parser
        parser = nouns[entry.noun]
        if entry.verb:
            parser = parser.add_parser(entry.verb, help="[verb] " + entry.description)
        if entry.handler in (project_hold, project_resume):
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
        if entry.verb in ('grant', 'revoke') and entry.noun == 'person':
            parser.add_argument('person_id')
            parser.add_argument('permission')
            parser.add_argument('--idempotency-key')
            parser.add_argument('--confirm')
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
    inputs = entry.input_model.model_validate({name: getattr(args, name) for name in entry.input_model.model_fields})
    assert entry.handler is not None
    outcome: Outcome
    try:
        if entry.noun == 'person' and entry.verb in ('grant', 'revoke') and not os.environ.get('T6_PREVIEW_SIGNING_KEY'):
            outcome = Refused(rule='config', reason='Set T6_PREVIEW_SIGNING_KEY')
        else:
            outcome = invoke(entry, inputs, actor=os.environ.get('T6_PERSON_ID', ''), people=_people(), open_notion=open_notion)
    except (OSError, ValueError) as error:
        outcome = Refused(rule='identity', reason=str(error))
    code, payload = outcome_json(outcome)
    if entry.noun == "departments" and not entry.verb:
        if isinstance(outcome, Done):
            payload = outcome.result.model_dump(mode="json")
        elif isinstance(outcome, Refused):
            from t6.cli import _fail
            return _fail(outcome.reason)
    if entry.handler is project_activity and not args.json and isinstance(outcome, Done):
        for item in outcome.result.entries:
            when = datetime.fromtimestamp(item.at, timezone.utc).isoformat()
            print(f"{when}  {item.actor}  {item.summary}")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code
