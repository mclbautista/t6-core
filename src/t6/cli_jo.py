"""Legacy Job Order reads and formatting for the `t6` CLI.

JO writes are registered studio verbs; this module handles `jo get`, `jo format`
and `deadline` and prepares the fields supplied to `jo raise`.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
from collections.abc import Callable
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from t6.cli import TOKEN_VARIABLE, _fail, _read_data_sources

if TYPE_CHECKING:
    from t6.notion.sync import NotionClient

JSON = dict[str, Any]


def add_parsers(commands: Any) -> None:
    def data_sources(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--data-sources", type=Path, metavar="FILE", help="JSON of database key to data source ID")

    jo = commands.add_parser("jo", help="Job Order commands")
    jo_commands = jo.add_subparsers(dest="jo_command", required=True)
    get = jo_commands.add_parser(
        "get",
        help="Print a Job Order by Notion page ID or Telegram Key",
        description=(
            f"Reads ${TOKEN_VARIABLE}. Needs a 'job_orders' entry in --data-sources. A Telegram Key is "
            "<chat_id>:<message_id>; a group chat ID is negative, so put it after `--`."
        ),
    )
    get.add_argument("ref", help="a Job Order page ID, or a Telegram Key")
    data_sources(get)
    fmt = jo_commands.add_parser(
        "format",
        help="Put a drafted Job Order into the studio layout (header, date line, defaults) and list what is missing",
        description=(
            "Reads a draft: a header line, `Label: value` lines, then free text. Prints {text, findings}. "
            "Needs no Notion access."
        ),
    )
    fmt.add_argument("--file", type=Path, required=True, metavar="FILE", help="the draft")
    fmt.add_argument("--date", type=date.fromisoformat, metavar="YYYY-MM-DD", help="the Job Order's date (default: today, Manila)")
    deadline = commands.add_parser(
        "deadline",
        help="Resolve a posted deadline phrase to an Asia/Manila time",
        description=(
            "Prints {deadline, display, source, findings}. 'end of shift' and 'asap' use the latest end of shift "
            f"of --assignees (Contact page IDs; reads ${TOKEN_VARIABLE} only when they are given)."
        ),
    )
    deadline.add_argument("phrase", help="the deadline as posted, for example 'Nov 13 5pm' or 'end of shift'")
    deadline.add_argument("--assignees", nargs="*", default=[], metavar="CONTACT_ID", help="the assignee Contact page IDs")
    deadline.add_argument("--person", help="Person T6 ID")
    deadline.add_argument("--posted", type=datetime.fromisoformat, metavar="ISO", help="when it was posted (default: now); no zone means Manila")


def run(args: argparse.Namespace, open_notion: Callable[[str], NotionClient]) -> int:
    try:
        if args.command == "jo" and args.jo_command == "format":
            return _print(_format(args.file.read_text(encoding="utf-8"), args.date))
        if args.command == "deadline" and args.person:
            from t6.verbs import _people
            person = _people().get(args.person)
            if person is None:
                return _fail("Person not found")
            if not args.assignees:
                return _print(_deadline(args.phrase, args.posted, (), person))
        if args.command == "deadline" and not args.assignees:
            return _print(_deadline(args.phrase, args.posted, ()))
        from t6.notion import NotionError
    except (OSError, ValueError) as error:
        return _fail(str(error))
    except ImportError:
        return _fail(f"{args.command} needs the notion extra: pip install 't6[notion]'")
    token = os.environ.get(TOKEN_VARIABLE)
    if not token:
        return _fail(f"set {TOKEN_VARIABLE} to a Notion integration token")
    try:
        with open_notion(token) as client:
            if args.command == "deadline":
                from t6.notion.reads import ContactReader

                reader = ContactReader(client, "")
                return _print(_deadline(args.phrase, args.posted, tuple(reader.get(ref) for ref in args.assignees), person if args.person else None))
            sources = _read_data_sources(args.data_sources)
            if "job_orders" not in sources:
                return _fail("--data-sources has no 'job_orders' entry")
            if args.jo_command == "get":
                return _print(_get(client, sources["job_orders"], args.ref))
            return _fail("unknown Job Order command")
    except (OSError, TypeError, ValueError, ImportError, NotionError) as error:  # TypeError: an unknown JO field
        return _fail(str(error))


def _print(result: JSON) -> int:
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _order(order: Any) -> JSON:
    fields: JSON = _jsonable(order)
    fields["id"] = fields.pop("page_id")
    fields.pop("external_refs", None)
    return fields


def _format(draft: str, day: date | None) -> JSON:
    from t6.domain.job_order_format import format_job_order
    from t6.domain.studio_time import MANILA

    result = format_job_order(draft, day or datetime.now(MANILA).date())
    return {"text": result.text, "findings": _jsonable(result.findings)}


def _deadline(phrase: str, posted: datetime | None, assignees: tuple[Any, ...], person: dict[str, Any] | None = None) -> JSON:
    from t6.domain import dateformats
    from t6.domain.deadlines import deadline
    from t6.domain.studio_time import MANILA

    posted = posted or datetime.now(MANILA)
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=MANILA)
    result = deadline(phrase, posted, assignees, person=person)
    return {
        "deadline": result.deadline.isoformat() if result.deadline else None,
        "display": dateformats.deadline(result.deadline) if result.deadline else None,
        "source": result.source,
        "findings": _jsonable(result.findings),
    }


def _get(client: NotionClient, data_source_id: str, ref: str) -> JSON:
    from t6.domain.job_order_rules import parse_telegram_key
    from t6.notion.job_orders import JobOrders

    from t6.job_order import JobOrderStore
    from t6.preview import PreviewTokens
    from t6.verbs import _people

    orders = JobOrders(client, data_source_id)
    try:
        people = _people()
        store = JobOrderStore(people.registry, PreviewTokens("read-only"))
        record = store.get(ref)
    except ValueError:
        record = None
    if record is not None:
        page = next((b["page_id"] for b in record["bindings"] if b["active"]), None)
        fields = _order(orders.get(page)) if page else dict(record["fields"])
        fields["notion_notes"] = fields.get("notes")
        fields.update(id=record["id"], t6_id=record["id"], page_id=page,
                      assignee_id=record["assignee_id"], assignee=people.get(record["assignee_id"]) if record["assignee_id"] else None, notes=record["notes"])
        return fields
    if parse_telegram_key(ref) is not None:
        found = orders.find_by_telegram_key(ref)
        if found is None:
            raise ValueError(f"no Job Order has the Telegram Key {ref}")
        return _order(orders.get(found.page_id))
    return _order(orders.get(ref))


def prepare_fields(client: NotionClient, data_source_id: str, path: Path) -> JSON:
    """Validate and normalize the same fields accepted by the original create command."""
    from t6.domain.studio_people import with_standing_cc
    from t6.domain.studio_time import END_OF_DAY, MANILA
    from t6.notion.departments import UnknownDepartment, get_departments, resolve_department
    from t6.notion.job_orders import FIELDS, RELATIONS

    fields: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(fields, dict):
        raise ValueError("the Job Order file must be a JSON object of fields")
    unknown = sorted(set(fields) - {*FIELDS, *RELATIONS, "project_id", "source_text"})
    if unknown:
        raise ValueError(f"not Job Order fields that can be written: {', '.join(unknown)}")
    for name in ("date", "deadline"):
        if isinstance(fields.get(name), str):
            text = fields[name]
            moment = datetime.fromisoformat(text) if "T" in text or " " in text else date.fromisoformat(text)
            if name == "deadline" and not isinstance(moment, datetime):
                moment = datetime.combine(moment, END_OF_DAY)
            if isinstance(moment, datetime) and moment.tzinfo is None:
                moment = moment.replace(tzinfo=MANILA)
            fields[name] = moment.isoformat()
    if fields.get("department"):
        department = resolve_department(get_departments(client, data_source_id), str(fields["department"]))
        if isinstance(department, UnknownDepartment):
            raise ValueError(f"unknown Department {department.token!r}; run `t6 departments` for the live ones")
        fields["department"] = department.name
    fields["cc_ids"] = list(with_standing_cc(fields.get("cc_ids") or ()))
    fields.setdefault("watermark", "none")
    return fields
