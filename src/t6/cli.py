"""JSON command-line interface for the shared library.

Every command prints one JSON document on stdout. Exit status: 0 for success,
1 when `schema-check` finds a problem, 2 when the command cannot run.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
import json
import os
from importlib.metadata import version
from pathlib import Path
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from t6.notion.sync import NotionClient

TOKEN_VARIABLE = "T6_NOTION_TOKEN"


def _open_notion(token: str) -> NotionClient:
    from t6.notion import NotionClient, RetryPolicy

    return NotionClient(token, retry=RetryPolicy())


def _fail(message: str) -> int:
    print(json.dumps({"error": message}), file=sys.stderr)
    return 2


def _schema_check(
    args: argparse.Namespace, open_notion: Callable[[str], NotionClient]
) -> int:
    try:
        from t6.notion.schema import check_schema, load_snapshot
    except ImportError:
        return _fail("schema-check needs the notion extra: pip install 't6[notion]'")
    token = os.environ.get(TOKEN_VARIABLE)
    if not token:
        return _fail(f"set {TOKEN_VARIABLE} to a Notion integration token")
    try:
        snapshot = load_snapshot(args.snapshot)
        data_sources: Any = json.loads(args.data_sources.read_text(encoding="utf-8"))
        if not isinstance(data_sources, dict) or not all(
            isinstance(value, str) for value in data_sources.values()
        ):
            return _fail("--data-sources must be a JSON object of database key to data source ID")
        with open_notion(token) as client:
            report = check_schema(client, snapshot, data_sources)
    except (OSError, ValueError) as error:
        return _fail(str(error))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


def main(
    argv: Sequence[str] | None = None,
    *,
    open_notion: Callable[[str], NotionClient] = _open_notion,
) -> int:
    """Run a T6 command and return its exit status."""
    parser = argparse.ArgumentParser(prog="t6")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("version", help="Print the installed version as JSON")
    check = commands.add_parser(
        "schema-check",
        help="Diff live Notion against the schema snapshot; report Project Code problems",
        description=(
            f"Reads the Notion token from ${TOKEN_VARIABLE}. Prints a JSON report and "
            "exits 1 if any property was added, removed or changed, or a Project Code "
            "is blank or duplicated."
        ),
    )
    check.add_argument(
        "--data-sources",
        type=Path,
        required=True,
        metavar="FILE",
        help="JSON object of snapshot database key to live data source ID",
    )
    check.add_argument(
        "--snapshot",
        type=Path,
        metavar="FILE",
        help="check against this snapshot instead of the bundled one",
    )
    args = parser.parse_args(argv)
    if args.command == "schema-check":
        return _schema_check(args, open_notion)
    print(json.dumps({"version": version("t6")}))
    return 0
