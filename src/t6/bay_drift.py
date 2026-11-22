"""The bay drift report: each bay's local copies against the project server (ADR 0008 decision 6, #380).

It only reports. Nothing here writes to Resolve, Notion, t6 or a bay. A copy is never called "identical": the report compares the
fields a local copy can show (modified time, timeline count) and says how they stand.

Until the project server is confirmed to store the same project UUID as a bay (ADR 0008, still to confirm), a local copy and a
server project are matched by folder path and name, and every report says so (`matched_by`).
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

MATCHED_BY = "folder_path_and_name"
MATCH_NOTE = ("Matched by folder path and name: it is not confirmed yet that the project server stores the same project UUID as a bay. "
              "A copy is compared by modified time and timeline count, which does not prove the contents are the same.")


@dataclass(frozen=True, slots=True, kw_only=True)
class ServerProject:
    """One project on the project server, as the drift report needs it.

    `last_modified` (unix seconds) and `timeline_count` are None when the inventory does not carry them; the report then says
    `cannot_compare` instead of guessing.
    """

    external_id: str  # `<database>/<folder path>/<name>`, the Resolve-project Media location's ref
    folder_path: str  # the folders above the project, joined by `/`; empty at the top level
    name: str
    uuid: str | None = None
    last_modified: int | None = None
    timeline_count: int | None = None


ServerInventory = Callable[[], Sequence[ServerProject]]


def from_resolve_project(project: Any) -> ServerProject:
    """A `t6.resolve.ResolveProject` as a server project. It carries identity only, so copies read `cannot_compare`."""
    return ServerProject(external_id=project.external_id, folder_path="/".join(project.folder_path), name=project.name, uuid=project.internal_id)


def copy_state(local: Mapping[str, Any], server: ServerProject | None, report_stale: bool) -> str:
    """How one local copy stands against the server project it was matched to."""
    if report_stale:
        return "report_stale"
    if server is None:
        return "not_on_server"
    if server.last_modified is None:
        return "cannot_compare"
    if local["last_modified"] > server.last_modified:
        return "local_newer"
    if local["last_modified"] < server.last_modified:
        return "local_older"
    if server.timeline_count is not None and local["timeline_count"] != server.timeline_count:
        return "differs"
    return "equal"


def drift(scans: Sequence[Mapping[str, Any]], inventory: Sequence[ServerProject], projects: Mapping[str, str],
          only: str | None = None) -> dict[str, Any]:
    """Per project, which Bays hold a local copy and how each stands against the server.

    `scans` are `bay scan list` entries; `projects` maps a server project's external ID to its t6 Project ID. With `only` (an
    external ID) the report covers that one project. A Bay with no report, or a skipped one, is listed under `unknown_bays`:
    its copies are not known, which is not the same as having none.
    """
    bays = [{"bay_id": scan["bay_id"], "state": scan["state"] if scan["state"] == "no_report" or scan["status"] == "ok" else scan["status"]}
            for scan in scans]
    unknown = [bay["bay_id"] for bay in bays if bay["state"] in ("no_report", "skipped_resolve_open")]
    by_key: dict[tuple[str, str], ServerProject] = {(p.folder_path, p.name): p for p in inventory}
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for key, server in by_key.items():
        if only is None or server.external_id == only:
            rows[key] = {"external_id": server.external_id, "project": projects.get(server.external_id), "folder_path": key[0], "name": key[1],
                         "on_server": True, "copies": []}
    for scan in scans:
        if scan["state"] == "no_report" or scan["status"] != "ok":
            continue  # nothing is known of this Bay's copies
        for local in scan["local_copies"]:
            key = (local["folder_path"], local["project_name"])
            if key not in rows:
                if only is not None or key in by_key:
                    continue
                rows[key] = {"external_id": None, "project": None, "folder_path": key[0], "name": key[1], "on_server": False, "copies": []}
            rows[key]["copies"].append({"bay_id": scan["bay_id"], "state": copy_state(local, by_key.get(key), scan["state"] == "stale"),
                                        "last_modified": local["last_modified"], "timeline_count": local["timeline_count"]})
    listed = []
    for key in sorted(rows):
        row = rows[key]
        row["copies"].sort(key=lambda copy: copy["bay_id"])
        row["unknown_bays"] = unknown
        listed.append(row)
    return {"matched_by": MATCHED_BY, "note": MATCH_NOTE, "bays": bays, "projects": listed}
