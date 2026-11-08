"""The committed schema snapshot, and the drift check against live Notion.

`schema_snapshot.json` lists every T6 database and the exact name and type of
each property. Option values are left out on purpose: they hold show and
client names, and this repository is public. The generated models in
`t6.notion.generated` come from the snapshot, and `check_schema` diffs live
Notion against it.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

from t6.notion import codec
from t6.notion.errors import NotionError

if TYPE_CHECKING:
    from t6.notion.sync import NotionClient

JSON = dict[str, Any]

PROJECT_CODE = "Project Code"
"""The Projects property that holds the human key of a Project."""


def load_snapshot(path: Path | None = None) -> JSON:
    """Read a schema snapshot: the bundled one, or the file at `path`."""
    if path is not None:
        return _as_snapshot(json.loads(path.read_text(encoding="utf-8")))
    text = resources.files("t6.notion").joinpath("schema_snapshot.json").read_text("utf-8")
    return _as_snapshot(json.loads(text))


def _as_snapshot(data: Any) -> JSON:
    if not isinstance(data, dict) or not isinstance(data.get("databases"), dict):
        raise ValueError("a schema snapshot is a JSON object with a 'databases' object")
    return data


def diff_properties(snapshot: list[JSON], live: JSON) -> JSON:
    """Compare snapshot properties with a live data source's `properties`.

    Names are compared exactly, trailing spaces and emoji included. A property
    is `changed` when its type differs.
    """
    live_types = {str(prop.get("name", key)): str(prop["type"]) for key, prop in live.items()}
    snapshot_types = {str(prop["name"]): str(prop["type"]) for prop in snapshot}
    return {
        "added": [
            {"name": name, "type": live_types[name]}
            for name in live_types
            if name not in snapshot_types
        ],
        "removed": [
            {"name": name, "type": snapshot_types[name]}
            for name in snapshot_types
            if name not in live_types
        ],
        "changed": [
            {"name": name, "snapshot_type": snapshot_types[name], "live_type": live_types[name]}
            for name in snapshot_types
            if name in live_types and live_types[name] != snapshot_types[name]
        ],
    }


def project_code_problems(pages: list[JSON]) -> JSON:
    """Blank and duplicate Project Codes among Projects pages."""
    blank: list[str] = []
    by_code: dict[str, list[str]] = {}
    for page in pages:
        code = codec.select((page.get("properties") or {}).get(PROJECT_CODE))
        if code is None or not code.strip():
            blank.append(str(page["id"]))
        else:
            by_code.setdefault(code.strip(), []).append(str(page["id"]))
    return {
        "checked": len(pages),
        "blank": blank,
        "duplicates": [
            {"project_code": code, "page_ids": ids}
            for code, ids in by_code.items()
            if len(ids) > 1
        ],
    }


def check_schema(
    client: NotionClient, snapshot: JSON, data_sources: dict[str, str]
) -> JSON:
    """Diff live Notion against the snapshot and audit Project Codes.

    `data_sources` maps snapshot database keys to live data source IDs. The
    report is plain JSON data. `ok` is true when nothing drifted and every
    Project Code is present and unique. A database with no data source ID is
    listed under `skipped`; it is not checked.
    """
    unknown = sorted(set(data_sources) - set(snapshot["databases"]))
    if unknown:
        raise ValueError(f"data sources for databases not in the snapshot: {', '.join(unknown)}")
    databases: dict[str, JSON] = {}
    for key, database in snapshot["databases"].items():
        if key not in data_sources:
            continue
        try:
            live = client.retrieve_data_source(data_sources[key])
        except NotionError as error:
            databases[key] = {
                "status": "error",
                "error": {"status": error.status, "code": error.code, "message": error.message},
            }
            continue
        diff = diff_properties(database["properties"], live.get("properties") or {})
        drifted = any(diff.values())
        databases[key] = {"status": "drift" if drifted else "ok", **diff}

    project_codes: JSON | None = None
    if "projects" in data_sources and databases["projects"]["status"] != "error":
        try:
            project_codes = project_code_problems(
                client.query_data_source(data_sources["projects"])
            )
        except NotionError as error:
            project_codes = {
                "error": {"status": error.status, "code": error.code, "message": error.message}
            }

    clean_codes = project_codes is None or (
        "error" not in project_codes
        and not project_codes["blank"]
        and not project_codes["duplicates"]
    )
    return {
        "ok": clean_codes and all(db["status"] == "ok" for db in databases.values()),
        "databases": databases,
        "skipped": [key for key in snapshot["databases"] if key not in data_sources],
        "project_codes": project_codes,
    }
