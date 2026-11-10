"""The committed schema snapshot, and the drift check against live Notion.

`schema_snapshot.json` lists every T6 database and the exact name and type of
each property. Option values are left out on purpose: they hold show and
client names, and this repository is public. The generated models in
`t6.notion.generated` come from the snapshot, and `check_schema` diffs live
Notion against it. It also audits the Projects pages against the Project
registry (`t6.registry.check`).
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

from t6.notion.errors import NotionError
from t6.registry import ProjectRegistry, RegistryUnavailable
from t6.registry.check import ProjectFindings, audit_projects

if TYPE_CHECKING:
    from t6.notion.sync import NotionClient

JSON = dict[str, Any]



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


def check_schema(
    client: NotionClient,
    snapshot: JSON,
    data_sources: dict[str, str],
    registry: ProjectRegistry | None = None,
) -> JSON:
    """Diff live Notion against the snapshot and audit the Projects against the registry.

    `data_sources` maps snapshot database keys to live data source IDs. The
    report is plain JSON data. A database with no data source ID is listed
    under `skipped`; it is not checked.

    `project_codes` audits the Projects data source against `registry`. A blank
    Project Code, a code several Projects kept on record and a Project out of
    rotation are all fine. Each real problem is a list of its own (see
    `t6.registry.check.KINDS`): active collisions, unclassified Projects,
    unbound pages, unavailable bindings, missing pages and projection
    conflicts. Its `status` is `ok`, `findings`, or `incomplete`.

    `incomplete` means an input could not be read: Notion, or the registry
    (`registry` is `None`, or raised `RegistryUnavailable`). The findings that
    could still be worked out are listed, `reasons` says what is missing, and
    `ok` is false: a clean report is never made from partial inputs.
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
        project_codes = _audit_projects(client, data_sources["projects"], registry)

    return {
        "ok": (project_codes is None or project_codes["status"] == "ok")
        and all(db["status"] == "ok" for db in databases.values()),
        "databases": databases,
        "skipped": [key for key in snapshot["databases"] if key not in data_sources],
        "project_codes": project_codes,
    }


def _error(source: str, error: NotionError) -> JSON:
    return {"source": source, "status": error.status, "code": error.code, "message": error.message}


def _audit_projects(client: NotionClient, data_source_id: str, registry: ProjectRegistry | None) -> JSON:
    reasons: list[JSON] = []
    pages: list[str] | None = None
    try:
        pages = [str(row["id"]) for row in client.query_data_source(data_source_id)]
    except NotionError as error:
        reasons.append(_error("notion", error))
    findings: ProjectFindings | None = None
    if registry is None:
        reasons.append({"source": "registry", "message": "no registry was given, or its file was not found"})
    else:
        try:
            findings = audit_projects(registry, pages)
        except RegistryUnavailable as error:
            reasons.append({"source": "registry", "message": str(error)})
    status = "incomplete" if reasons else "ok" if findings is not None and findings.empty else "findings"
    return {
        "status": status,
        "checked": None if pages is None else len(pages),
        "findings": None if findings is None else findings.to_json(),
        "reasons": reasons,
    }
