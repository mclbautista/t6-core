"""Read the team-maintained Package Specs data source."""

from __future__ import annotations

from dataclasses import dataclass

from t6.notion import codec
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
            filter={"property": "Package kind", "rich_text": {"equals": package_kind}},
        )
        result = []
        for page in pages:
            if not is_live(page):
                continue
            props = page["properties"]
            kind = codec.rich_text(props.get("Package kind"))
            client = codec.rich_text(props.get("Client spec"))
            if kind != package_kind or client != client_spec:
                continue
            version = codec.rich_text(props.get("Version"))
            if not version or not version.strip():
                raise ValueError(f"Package Spec {page['id']} has no version")
            items = tuple(
                ChecklistItem(line.strip(), blocking)
                for name, blocking in (("Advisory items", False), ("Blocking items", True))
                for line in (codec.rich_text(props.get(name)) or "").splitlines()
                if line.strip()
            )
            result.append(PackageSpec(page["id"], kind, client, version, items))
        return tuple(result)
