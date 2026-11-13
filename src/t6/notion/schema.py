"""The committed schema snapshot, and the drift check against live Notion.

`schema_snapshot.json` lists every T6 database and the Notion ID, exact name
and type of each property. Select, multi-select and status properties also list
their option IDs. Option names are left out on purpose: they hold show and
client names, and this repository is public. The ID is the key: a property
renamed in Notion keeps its ID, so a rename needs no code change.
`refresh_snapshot` rebuilds the snapshot from live Notion. The generated models in
`t6.notion.generated` come from the snapshot, and `check_schema` diffs live
Notion against it. It also audits the Projects pages against the Project
registry (`t6.registry.check`).
"""

from __future__ import annotations

import copy
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

    A snapshot property with an ID is matched to the live property with that
    ID, so a rename is reported under `renamed` (information, not drift). One
    with no ID is matched by exact name, trailing spaces and emoji included.
    `added` and `removed` are drift, and so is a `changed` type.
    """
    live_props = [{**prop, "name": str(prop.get("name", key))} for key, prop in live.items()]
    live_by_id = {str(prop["id"]): prop for prop in live_props if "id" in prop}
    live_by_name = {prop["name"]: prop for prop in live_props}
    matched: set[str] = set()
    removed, changed, renamed = [], [], []
    for prop in snapshot:
        name, kind = str(prop["name"]), str(prop["type"])
        found = live_by_id.get(str(prop["id"])) if prop.get("id") else live_by_name.get(name)
        if found is None:
            removed.append({"name": name, "type": kind})
            continue
        matched.add(found["name"])
        if found["name"] != name:
            renamed.append({"from": name, "to": found["name"]})
        if str(found["type"]) != kind:
            changed.append({"name": name, "snapshot_type": kind, "live_type": str(found["type"])})
    added = [
        {"name": prop["name"], "type": str(prop["type"])}
        for prop in live_props
        if prop["name"] not in matched
    ]
    return {"added": added, "removed": removed, "changed": changed, "renamed": renamed}


def refresh_snapshot(client: NotionClient, snapshot: JSON, data_sources: dict[str, str]) -> JSON:
    """A copy of `snapshot` with property and option IDs (and names) read from live Notion.

    For each database in `data_sources`, a property keeps its place when its ID
    (or, before it has one, its name) is still live; a live property the
    snapshot lacks is appended; a property no longer live is dropped. Databases
    not in `data_sources` are copied as they are. Option IDs are kept, names
    are not.
    """
    unknown = sorted(set(data_sources) - set(snapshot["databases"]))
    if unknown:
        raise ValueError(f"data sources for databases not in the snapshot: {', '.join(unknown)}")
    refreshed: JSON = copy.deepcopy(snapshot)
    for key, source_id in data_sources.items():
        live = client.retrieve_data_source(source_id).get("properties") or {}
        live_props = [{**prop, "name": str(prop.get("name", name))} for name, prop in live.items()]
        by_id = {str(prop["id"]): prop for prop in live_props if "id" in prop}
        by_name = {prop["name"]: prop for prop in live_props}
        kept: list[JSON] = []
        seen: set[str] = set()
        for prop in refreshed["databases"][key]["properties"]:
            found = by_id.get(str(prop["id"])) if prop.get("id") else by_name.get(str(prop["name"]))
            if found is not None and found["name"] not in seen:
                seen.add(found["name"])
                kept.append(_snapshot_property(found))
        kept += [_snapshot_property(prop) for prop in live_props if prop["name"] not in seen]
        refreshed["databases"][key]["properties"] = kept
    return refreshed


def _snapshot_property(live: JSON) -> JSON:
    entry: JSON = {"name": live["name"], "type": str(live["type"])}
    if "id" in live:
        entry["id"] = str(live["id"])
    options = (live.get(live["type"]) or {}).get("options")
    if isinstance(options, list) and live["type"] in ("select", "multi_select", "status"):
        entry["options"] = [str(option["id"]) for option in options if "id" in option]
    return entry


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
        drifted = any(diff[kind] for kind in ("added", "removed", "changed"))
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


def fix_snapshot(snapshot: JSON, report: JSON) -> JSON:
    """A copy of `snapshot` with property names updated for the renames in `report`.

    `report` is a `check_schema` report. Only renames (same ID, new name) are
    applied. A property added, removed or changed in type, or a database that
    could not be read, raises ValueError and nothing is returned: that needs a
    t6-core change, not a snapshot refresh.
    """
    refused = [
        key
        for key, database in report["databases"].items()
        if database["status"] != "ok"
    ]
    if refused:
        raise ValueError(
            f"not only renames in: {', '.join(refused)}; a property was added, removed or changed"
        )
    fixed: JSON = copy.deepcopy(snapshot)
    for key, database in report["databases"].items():
        by_name = {str(prop["name"]): prop for prop in fixed["databases"][key]["properties"]}
        for rename in database["renamed"]:
            by_name[rename["from"]]["name"] = rename["to"]
    return fixed


def drift_markdown(report: JSON) -> str:
    """The drift in a `check_schema` report as Markdown for an issue, or "" when there is none.

    Lists database keys, property names and types, and nothing else: no option
    names, Project Codes or IDs. The Projects audit is left out; it needs the
    registry, which a scheduled run does not have.
    """
    lines: list[str] = []
    for key, database in sorted(report["databases"].items()):
        if database["status"] == "ok":
            continue
        lines.append(f"### {key}")
        if database["status"] == "error":
            lines.append(f"- could not be read (HTTP {database['error']['status']}, {database['error']['code']})")
            continue
        lines += [f"- added: `{p['name']}` ({p['type']})" for p in database["added"]]
        lines += [f"- removed: `{p['name']}` ({p['type']})" for p in database["removed"]]
        lines += [
            f"- type changed: `{p['name']}` ({p['snapshot_type']} to {p['live_type']})"
            for p in database["changed"]
        ]
    if not lines:
        return ""
    renamed = [
        f"- `{rename['from']}` to `{rename['to']}` ({key})"
        for key, database in sorted(report["databases"].items())
        for rename in database.get("renamed", [])
    ]
    body = ["Live Notion no longer matches the schema snapshot.", "", *lines]
    if renamed:
        body += ["", "Renamed (no drift; `t6 schema-check --fix-snapshot` updates the names):", *renamed]
    body += [
        "",
        "A deleted or new property needs a t6-core change, then `t6 schema-snapshot` and "
        "`scripts/generate_models.py`.",
    ]
    return "\n".join(body) + "\n"


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
