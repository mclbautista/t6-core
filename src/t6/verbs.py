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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel


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
    handler: Callable[..., Outcome]


VERBS = (
    Verb("departments", "", "Print the studio Departments, read live, in display order, with option IDs", (),
         DepartmentsInput, DepartmentsOutput, "reader", "Notion Department options are the truth; use studio display order.", departments),
)


def catalogue() -> list[dict[str, Any]]:
    return [{"noun": v.noun, "verb": v.verb, "description": v.description, "aliases": list(v.aliases),
             "role": v.role, "rule": v.rule, "input_schema": v.input_model.model_json_schema(),
             "output_schema": v.output_model.model_json_schema()} for v in VERBS]


def add_parsers(commands: Any) -> None:
    nouns: dict[str, Any] = {}
    for entry in VERBS:
        if entry.noun not in nouns:
            parser = commands.add_parser(entry.noun, help=("[verb] " if not entry.verb else "") + entry.description)
            nouns[entry.noun] = parser.add_subparsers(dest=f"{entry.noun}_verb", required=True) if entry.verb else parser
        parser = nouns[entry.noun]
        if entry.verb:
            parser = parser.add_parser(entry.verb, help="[verb] " + entry.description)
        for name, field in entry.input_model.model_fields.items():
            parser.add_argument("--" + name.replace("_", "-"), type=field.annotation, required=field.is_required(),
                                default=None if field.is_required() else field.default)
        parser.set_defaults(registered_verb=entry)
    commands_parser = commands.add_parser("commands", help="List registered verbs")
    commands_parser.add_argument("--json", action="store_true", required=True, help="Print the verb catalogue as JSON")


def run(args: argparse.Namespace, open_notion: Callable[..., Any]) -> int:
    entry: Verb = args.registered_verb
    inputs = entry.input_model.model_validate({name: getattr(args, name) for name in entry.input_model.model_fields})
    outcome = entry.handler(inputs, open_notion)
    code, payload = outcome_json(outcome)
    if entry.noun == "departments" and not entry.verb:
        if isinstance(outcome, Done):
            payload = outcome.result.model_dump(mode="json")
        elif isinstance(outcome, Refused):
            from t6.cli import _fail
            return _fail(outcome.reason)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code
