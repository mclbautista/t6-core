"""JSON command-line interface for the shared library.

Every command prints one JSON document on stdout. Exit status: 0 for success,
1 when `schema-check` finds a problem or a registry command reports one (a page
that could not be registered, a classification that was refused), 2 when the
command cannot run.
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
REGISTRY_DB_VARIABLE = "T6_REGISTRY_DB"
DEFAULT_REGISTRY_DB = "t6-registry.sqlite"


def _open_notion(token: str) -> NotionClient:
    from t6.notion import NotionClient, RetryPolicy

    return NotionClient(token, retry=RetryPolicy())


def _fail(message: str) -> int:
    print(json.dumps({"error": message}), file=sys.stderr)
    return 2


def _read_data_sources(path: Path) -> dict[str, str]:
    data_sources: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data_sources, dict) or not all(
        isinstance(value, str) for value in data_sources.values()
    ):
        raise ValueError("--data-sources must be a JSON object of database key to data source ID")
    return data_sources


def _cameras(
    args: argparse.Namespace, open_notion: Callable[[str], NotionClient]
) -> int:
    try:
        from t6.notion import NotionError
        from t6.notion.cameras import CameraInputError, get_cameras, update_camera
    except ImportError:
        return _fail("cameras needs the notion extra: pip install 't6[notion]'")
    token = os.environ.get(TOKEN_VARIABLE)
    if not token:
        return _fail(f"set {TOKEN_VARIABLE} to a Notion integration token")
    try:
        data_source_id = _read_data_sources(args.data_sources).get("cameras")
        if not data_source_id:
            return _fail("--data-sources has no 'cameras' entry")
        payload: Any = None
        if args.cameras_command == "update":
            text = args.json.read_text(encoding="utf-8") if args.json else sys.stdin.read()
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as error:
                return _fail(f"update input is not valid JSON: {error}")
        with open_notion(token) as client:
            if args.cameras_command == "get":
                result: Any = get_cameras(client, data_source_id, page_id=args.page_id)
            else:
                result = update_camera(client, data_source_id, args.page_id, payload)
    except (OSError, ValueError, NotionError) as error:  # CameraInputError is a ValueError
        return _fail(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _open_registry(path: Path) -> Any:
    """The registry at `path`, or `None` when there is no file or it cannot be opened.

    A missing file is not created: an empty registry would make every page look
    unbound. The check reports `None` as an incomplete audit.
    """
    from t6.registry import RegistryUnavailable, SqliteProjectRegistry

    if not path.is_file():
        return None
    try:
        return SqliteProjectRegistry(path)
    except RegistryUnavailable:
        return None


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
        data_sources = _read_data_sources(args.data_sources)
        registry = _open_registry(args.registry_db)
        with open_notion(token) as client:
            report = check_schema(client, snapshot, data_sources, registry)
    except (OSError, ValueError) as error:
        return _fail(str(error))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


def _migrate_projects(
    args: argparse.Namespace, open_notion: Callable[[str], NotionClient]
) -> int:
    try:
        from t6.notion.migration import read_legacy_projects
        from t6.registry import SqliteProjectRegistry
        from t6.registry.migration import PreviewMismatch, apply, preview
    except ImportError:
        return _fail("migrate-projects needs the notion extra: pip install 't6[notion]'")
    token = os.environ.get(TOKEN_VARIABLE)
    if not token:
        return _fail(f"set {TOKEN_VARIABLE} to a Notion integration token")
    if args.apply and not (args.preview and args.actor):
        return _fail("--apply needs --preview FILE (the exported preview) and --actor NAME")
    try:
        registry = SqliteProjectRegistry(args.registry_db)
        with open_notion(token) as client:
            projects, not_counted = read_legacy_projects(client, _read_data_sources(args.data_sources))
        if not args.apply:
            print(json.dumps(preview(registry, projects, not_counted=not_counted).to_json(), ensure_ascii=False, indent=2))
            return 0
        exported: Any = json.loads(args.preview.read_text(encoding="utf-8"))
        report = apply(registry, projects, actor=args.actor, expected_digest=str(exported["digest"]))
    except PreviewMismatch as error:
        return _fail(str(error))
    except (OSError, ValueError, KeyError, TypeError) as error:
        return _fail(str(error))
    print(json.dumps(report.to_json(), ensure_ascii=False, indent=2))
    return 0 if report.ok else 1


def _classify_projects(args: argparse.Namespace) -> int:
    from t6.registry import RegistryError, Rotation, SqliteProjectRegistry
    from t6.registry.migration import classify

    try:
        decisions: Any = json.loads(args.decisions.read_text(encoding="utf-8"))
        if not isinstance(decisions, dict):
            raise ValueError("--decisions must be a JSON object of T6 ID or page ID to 'in' or 'out'")
        report = classify(
            SqliteProjectRegistry(args.registry_db),
            {str(ref): Rotation(str(value)) for ref, value in decisions.items()},
            actor=args.actor,
        )
    except (OSError, ValueError, RegistryError) as error:  # a bad rotation is a ValueError
        return _fail(str(error))
    print(json.dumps(report.to_json(), ensure_ascii=False, indent=2))
    return 0 if report.ok else 1


def _drives_import_dry_run(args: argparse.Namespace) -> int:
    try:
        from t6.drives.importer import import_log_sheet_workbook, import_per_project_workbook
    except ImportError:
        return _fail("drives-import-dry-run needs the drives extra: pip install 't6[drives]'")
    from dataclasses import asdict

    from t6.domain import Project
    from t6.drives import Ref
    from t6.drives.testing import InMemoryDriveStore
    from t6.lookup import Index, Lookup

    try:
        listed: Any = json.loads(args.projects.read_text(encoding="utf-8")) if args.projects else []
        projects = tuple(Project(page_id=p["page_id"], name=p["name"]) for p in listed)
        store, lookup, logged_by = InMemoryDriveStore(), Lookup(Index(projects=projects)), Ref(text="dry run")
        results = [import_per_project_workbook(args.workbook, store, lookup=lookup, logged_by=logged_by)]
        if args.log_sheet:
            results.append(import_log_sheet_workbook(args.log_sheet, store, lookup=lookup, logged_by=logged_by))
    except (OSError, ValueError, KeyError, TypeError) as error:
        return _fail(str(error))
    report = {
        "imported": sum(result.imported for result in results),
        "review": [asdict(item) for result in results for item in result.review],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _database_paths() -> dict[str, Path]:
    """The server's three database files, from the same variables and defaults it uses."""
    return {
        "registry": Path(os.environ.get(REGISTRY_DB_VARIABLE, "t6-registry.sqlite")),
        "queue": Path(os.environ.get("T6_QUEUE_DB", "t6-queue.sqlite")),
        "operator": Path(os.environ.get("T6_OPERATOR_DB", "t6-operator.sqlite")),
    }


def _ops(args: argparse.Namespace) -> int:
    from t6 import ops

    try:
        if args.command == "backup":
            result: Any = ops.backup(_database_paths(), args.into)
        elif args.command == "restore":
            result = ops.restore(args.source, _database_paths())
        else:
            result = ops.health(_database_paths())
    except (OSError, ops.OpsError) as error:
        return _fail(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if args.command != "health" or result["ok"] else 1


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
        help="Diff live Notion against the schema snapshot; audit Projects against the registry",
        description=(
            f"Reads the Notion token from ${TOKEN_VARIABLE} and the registry file from "
            f"--registry-db (default ${REGISTRY_DB_VARIABLE}). Prints a JSON report and exits 1 if any "
            "property was added, removed or changed, if the Projects audit has findings, or if the "
            "audit is incomplete because Notion or the registry could not be read. A blank or "
            "reused Project Code is not a problem."
        ),
    )
    check.add_argument(
        "--registry-db",
        type=Path,
        default=Path(os.environ.get(REGISTRY_DB_VARIABLE, DEFAULT_REGISTRY_DB)),
        metavar="FILE",
        help="the Project registry file to audit the Projects against",
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
    cameras = commands.add_parser(
        "cameras",
        help="Read and update Cameras as JSON in domain field names",
        description=(
            f"Reads the Notion token from ${TOKEN_VARIABLE}. Prints JSON. Exits 2 with a "
            "JSON error on stderr for unknown fields, invalid values or Notion errors."
        ),
    )
    cameras_commands = cameras.add_subparsers(dest="cameras_command", required=True)
    get = cameras_commands.add_parser("get", help="Print Cameras (or one) as a JSON list")
    get.add_argument("--page-id", help="print only this Camera")
    update = cameras_commands.add_parser(
        "update", help="Apply a JSON object of fields to one Camera; print the updated row"
    )
    update.add_argument("page_id", help="Notion page ID of the Camera")
    update.add_argument(
        "--json", type=Path, metavar="FILE", help="read the update from FILE instead of stdin"
    )
    for sub in (get, update):
        sub.add_argument(
            "--data-sources",
            type=Path,
            required=True,
            metavar="FILE",
            help="JSON object of snapshot database key to live data source ID",
        )
    registry_db = os.environ.get(REGISTRY_DB_VARIABLE, DEFAULT_REGISTRY_DB)
    migrate = commands.add_parser(
        "migrate-projects",
        help="Preview, or apply, registering the existing Notion Projects in the Project registry",
        description=(
            f"Reads the Notion token from ${TOKEN_VARIABLE} and the registry file from "
            f"--registry-db (default ${REGISTRY_DB_VARIABLE}). Without --apply it prints a preview and "
            "writes nothing. With --apply --preview FILE it registers the pages in that preview, "
            "unclassified, and prints a report; it refuses if the pages changed since the preview."
        ),
    )
    migrate.add_argument("--data-sources", type=Path, required=True, metavar="FILE", help="JSON object of 'projects' (and optionally 'job_orders', 'deliverables', 'external_links') to data source ID")
    migrate.add_argument("--registry-db", type=Path, default=Path(registry_db), metavar="FILE")
    migrate.add_argument("--apply", action="store_true", help="register the Projects in --preview")
    migrate.add_argument("--preview", type=Path, metavar="FILE", help="the preview this run applies")
    migrate.add_argument("--actor", help="who is applying, for the audit history")
    classify_projects = commands.add_parser(
        "classify-projects",
        help="Set unclassified Projects in or out of rotation, as an operator decided",
        description=(
            "--decisions is a JSON object of T6 Project ID (or Notion page ID) to 'in' or 'out'. "
            "Only the Projects named change, and only if they are unclassified."
        ),
    )
    classify_projects.add_argument("--decisions", type=Path, required=True, metavar="FILE")
    classify_projects.add_argument("--registry-db", type=Path, default=Path(registry_db), metavar="FILE")
    classify_projects.add_argument("--actor", required=True, help="who decided, for the audit history")
    commands.add_parser(
        "health",
        help="Report sync lag, persistent Notion failures and failed jobs; exits 1 on a problem",
        description="Reads the server's databases (T6_REGISTRY_DB, T6_QUEUE_DB, T6_OPERATOR_DB) and changes nothing.",
    )
    backup = commands.add_parser(
        "backup",
        help="Snapshot the registry, queue and operator databases into a new directory",
        description="Safe while the server runs. Writes a manifest with checksums and counts.",
    )
    backup.add_argument("--into", type=Path, required=True, metavar="DIR", help="an empty or new directory")
    restore = commands.add_parser(
        "restore",
        help="Restore a backup onto databases that do not exist yet (stop the server first)",
        description="Verifies checksums and counts first, never overwrites, and marks unconfirmed Notion syncs for dispatch again.",
    )
    restore.add_argument("--from", dest="source", type=Path, required=True, metavar="DIR", help="a directory made by backup")
    dry_run = commands.add_parser(
        "drives-import-dry-run",
        help="Import the legacy drive workbooks into memory and print the review report",
        description="Nothing is written anywhere: the import runs into an in-memory store that is thrown away.",
    )
    dry_run.add_argument("workbook", type=Path, help="the per-project drive workbook (.xlsx)")
    dry_run.add_argument("--log-sheet", type=Path, metavar="FILE", help="also import the drive log sheet workbook (.xlsx) into the same store")
    dry_run.add_argument("--projects", type=Path, metavar="FILE", help="JSON list of {\"page_id\", \"name\"} to resolve Project names against; without it every Project is unresolved")
    args = parser.parse_args(argv)
    if args.command == "drives-import-dry-run":
        return _drives_import_dry_run(args)
    if args.command in {"health", "backup", "restore"}:
        return _ops(args)
    if args.command == "migrate-projects":
        return _migrate_projects(args, open_notion)
    if args.command == "classify-projects":
        return _classify_projects(args)
    if args.command == "schema-check":
        return _schema_check(args, open_notion)
    if args.command == "cameras":
        return _cameras(args, open_notion)
    print(json.dumps({"version": version("t6")}))
    return 0
