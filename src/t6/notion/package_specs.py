"""Read the team-maintained Package Specs data source."""

from __future__ import annotations

from dataclasses import dataclass

from t6.notion.generated import PackageSpecsRow
from t6.notion.reads import is_live
from t6.notion.sync import NotionClient


@dataclass(frozen=True, slots=True)
class ChecklistItem:
    text: str
    blocking: bool = False


@dataclass(frozen=True, slots=True)
class PackageSpec:
    page_id: str
    package_kind: str
    client_spec: str | None
    version: str
    items: tuple[ChecklistItem, ...]


class PackageSpecReader:
    """Read specs for an exact package kind and optional client spec."""

    def __init__(self, client: NotionClient, data_source_id: str) -> None:
        self._client, self._source = client, data_source_id

    def find(self, package_kind: str, client_spec: str | None = None) -> tuple[PackageSpec, ...]:
        """Return all matching live rows; duplicates remain visible to the caller."""
        pages = self._client.query_data_source(
            self._source,
            filter={"property": PackageSpecsRow.notion_id("package_kind"), "rich_text": {"equals": package_kind}},
        )
        result = []
        for page in pages:
            if not is_live(page):
                continue
            row = PackageSpecsRow.from_page(page)
            if row.package_kind != package_kind or row.client_spec != client_spec:
                continue
            if not row.version or not row.version.strip():
                raise ValueError(f"Package Spec {page['id']} has no version")
            items = tuple(
                ChecklistItem(line.strip(), blocking)
                for text, blocking in ((row.advisory_items, False), (row.blocking_items, True))
                for line in (text or "").splitlines()
                if line.strip()
            )
            result.append(PackageSpec(page["id"], row.package_kind, row.client_spec, row.version, items))
        return tuple(result)
