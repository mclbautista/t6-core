"""Read the existing Notion Projects, and the rows that point at them, for the registry migration.

Needs the `notion` extra. Reads only. A row's relation to a Project is counted
by the Project page ID it holds, never by code or name.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from typing import Any

from t6.notion import codec
from t6.notion.external_links import ExternalLinksProperties
from t6.notion.generated import DeliverablesRow, JobOrdersRow
from t6.notion.models import project_from_page
from t6.notion.sync import NotionClient
from t6.registry import normalize_page_id
from t6.registry.migration import LegacyProject

__all__ = ["read_legacy_projects"]

JSON = dict[str, Any]


def read_legacy_projects(
    client: NotionClient, data_sources: Mapping[str, str]
) -> tuple[list[LegacyProject], list[str]]:
    """Every Project page, with how many rows of each kind point at it.

    `data_sources` maps `projects` (required), and optionally `job_orders`,
    `deliverables` and `external_links`, to data source IDs. Episodes are the
    Project's `Eps`. Also returns the reference kinds that were not counted
    because their data source was not given.
    """
    if "projects" not in data_sources:
        raise ValueError("data sources need a 'projects' entry")
    counts: dict[str, Counter[str]] = {}
    not_counted: list[str] = []
    readers: dict[str, Callable[[JSON], tuple[str, ...]]] = {
        "job_orders": lambda row: JobOrdersRow.from_page(row).projects,
        "deliverables": lambda row: DeliverablesRow.from_page(row).projects,
        "external_links": lambda row: codec.relation((row.get("properties") or {}).get(ExternalLinksProperties.PROJECTS)),
    }
    for kind, read in readers.items():
        source = data_sources.get(kind)
        if not source:
            not_counted.append(kind)
            continue
        found: Counter[str] = Counter()
        for row in client.query_data_source(source):
            for page in set(normalize_page_id(p) for p in read(row)):
                found[page] += 1
        counts[kind] = found
    projects: list[LegacyProject] = []
    for row in client.query_data_source(data_sources["projects"]):
        project = project_from_page(row)
        page = normalize_page_id(project.page_id)
        references = {kind: found[page] for kind, found in counts.items()}
        references["episodes"] = len(project.episodes)
        projects.append(LegacyProject(project.page_id, project.name, project.project_code, references))
    return projects, not_counted
