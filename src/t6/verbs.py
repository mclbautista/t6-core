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
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, get_args, get_origin
from types import UnionType

from pydantic import BaseModel, Field, RootModel


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
    Verb("project", "activity", "Show Project activity, newest first", (),
         ActivityInput, ActivityOutput, "reader", "Activity is append-only.", project_activity),
    Verb("jo", "get", "Get a Job Order", (), JoGetInput, LegacyOutput, "reader", "Read a Job Order.", None),
    Verb("jo", "raise", "Raise a Job Order", ("jo create",), JoRaiseInput, LegacyOutput, "writer", "Create a Job Order.", None),
    Verb("jo", "format", "Format a Job Order", (), JoFormatInput, LegacyOutput, "reader", "Format a draft.", None),
    Verb("person", "find", "Find a Person", ("contacts find",), PersonFindInput, LegacyOutput, "reader", "Find a Contact.", None),
    Verb("project", "resolve", "Resolve a Project", ("projects resolve",), ProjectResolveInput, LegacyOutput, "reader", "Resolve a Project.", None),
    Verb("camera", "get", "Get Cameras", ("cameras get",), CameraGetInput, LegacyOutput, "reader", "Read Cameras.", None),
    Verb("camera", "enrich", "Enrich a Camera", ("cameras update",), CameraEnrichInput, LegacyOutput, "writer", "Update a Camera.", None),
    Verb("departments", "", "Print the studio Departments, read live, in display order, with option IDs", (),
         DepartmentsInput, DepartmentsOutput, "reader", "Notion Department options are the truth; use studio display order.", departments),
)


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
            if entry.noun == "project":
                parser = commands.choices["projects"]
                nouns[entry.noun] = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
            else:
                parser = commands.add_parser(entry.noun, help=("[verb] " if not entry.verb else "") + entry.description)
                nouns[entry.noun] = parser.add_subparsers(dest=f"{entry.noun}_verb", required=True) if entry.verb else parser
        parser = nouns[entry.noun]
        if entry.verb:
            parser = parser.add_parser(entry.verb, help="[verb] " + entry.description)
        if entry.handler is project_activity:
            parser.add_argument("project")
            parser.add_argument("--since", help="ISO 8601 date or timestamp (UTC if no offset)")
            parser.add_argument("--limit", type=int)
            parser.add_argument("--json", action="store_true", help="Print JSON")
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
    outcome = entry.handler(inputs, open_notion)
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
