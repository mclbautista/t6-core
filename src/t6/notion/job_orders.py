"""Job Orders in studio terms: `t6.domain.JobOrder` in and out, no Notion property names.

`JobOrders` reads and creates Job Orders in one data source. A Job Order is a
`JobOrder` with every field the apps use (see the class for the fields). The
Notion names, including `Blocker ` with its trailing space, stay in
`t6.notion.generated.JobOrdersRow`; fields are addressed by property ID.

`create` saves through `t6.notion.writes`: it returns the saved `JobOrder`,
`Rejected`, or `Ambiguous`. The page body (`source_text`) is written as
paragraph blocks and read back. Credentials are withheld in text (see
`t6.notion.client`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from t6.domain import JobOrder
from t6.notion import codec, named
from t6.notion.errors import NotionError
from t6.notion.generated import JobOrdersRow
from t6.notion.reads import is_live, page_edited
from t6.notion.schema import load_snapshot
from t6.notion.sync import NotionClient
from t6.notion.writes import Ambiguous, Rejected, create_row
from t6.privacy.withhold import withhold

JSON = dict[str, Any]

DATABASE = "job_orders"
FIELDS: dict[str, str] = {
    "number": "jo_number",
    "department": "department",
    "date": "date",
    "deadline": "deadline",
    "title": "title",
    "subtitle": "subtitle",
    "task": "task",
    "purpose": "purpose",
    "source": "source_source_drive",
    "platform": "platform",
    "link_settings": "link_settings",
    "watermark": "watermark",
    "mode_of_transfer": "mode_of_transfer",
    "blocker": "blocker",
    "pending": "pending",
    "notes": "jo_notes",
    "watchdog_notes": "watchdog_notes",
    "telegram_key": "telegram_key",
    "status": "status",
    "name": "name",
}
"""Domain field to `JobOrdersRow` attribute, for the fields that map one to one."""
RELATIONS: dict[str, str] = {
    "assigned_ids": "assigned",
    "cc_ids": "cc",
    "coordinator_ids": "coordinator",
    "deliverable_ids": "deliverable",
}
UNMAPPED: frozenset[str] = frozenset({"place"})
"""Job Orders properties no `JobOrder` field carries on purpose. `place` is a location Notion adds."""
READ_ONLY = frozenset({"health"})
BLOCK_TEXT_LIMIT = 2000
"""The most characters Notion takes in one rich-text part."""


class JobOrders:
    """Reads and creates Job Orders in one data source."""

    def __init__(self, client: NotionClient, data_source_id: str, snapshot: JSON | None = None) -> None:
        self._client = client
        self._source = data_source_id
        self._snapshot = snapshot or load_snapshot()

    def get(self, page_id: str) -> JobOrder:
        """The Job Order on a page, with its body."""
        return self._read(self._client.retrieve_page(page_id))

    def find_by_telegram_key(self, telegram_key: str) -> JobOrder | None:
        """The Job Order whose Telegram Key is exactly `telegram_key`, or None. The first one, if several."""
        rows = self._client.query_data_source(
            self._source,
            filter={
                "property": JobOrdersRow.notion_id("telegram_key") or JobOrdersRow.notion_name("telegram_key"),
                "rich_text": {"equals": telegram_key},
            },
        )
        return self._read(rows[0]) if rows else None

    def list(self) -> tuple[JobOrder, ...]:
        """Every Job Order that is not trashed or archived, without bodies."""
        pages = self._client.query_data_source(self._source)
        return tuple(self._order(JobOrdersRow.from_page(page), page) for page in pages if is_live(page))

    def create(self, fields: Mapping[str, Any]) -> JobOrder | Rejected | Ambiguous:
        """Create a Job Order from domain fields and return it as saved.

        `fields` may hold any `JobOrder` field except `page_id` and `health`,
        and `project_id` for the Project, and `source_text` for the page body.
        An unknown field raises TypeError. A body that could not be saved,
        or does not read back as written, is `Ambiguous`: the page exists.
        """
        allowed = {*FIELDS, *RELATIONS, "project_id", "source_text"}
        unknown = sorted(set(fields) - allowed)
        if unknown:
            raise TypeError(f"not Job Order fields that can be written: {', '.join(unknown)}")
        values: JSON = {FIELDS[name]: value for name, value in fields.items() if name in FIELDS}
        for attribute in ("status", "department"):
            if attribute in values:
                values[attribute] = named.to_option(self._snapshot, DATABASE, attribute, values[attribute])
        values.update({RELATIONS[name]: tuple(fields[name]) for name in RELATIONS if name in fields})
        if fields.get("project_id"):
            values["projects"] = (fields["project_id"],)
        saved = create_row(self._client, JobOrdersRow, self._source, values)
        if isinstance(saved, (Rejected, Ambiguous)):
            return saved
        body = fields.get("source_text")
        if body:
            failed = self._write_body(saved.page_id, str(body))
            if failed is not None:
                return failed
        return self.get(saved.page_id)

    def _write_body(self, page_id: str, text: str) -> Ambiguous | None:
        expected = withhold(text).text
        try:
            self._client.append_block_children(page_id, _blocks(text))
            saved = _body_text(self._client.list_block_children(page_id))
        except NotionError as error:
            reason = error.code if error.code in ("timeout", "network_error") else "readback_failed"
            return Ambiguous(reason, f"the page was created but its body was not saved: {error}", page_id)  # type: ignore[arg-type]
        if saved != expected:
            return Ambiguous("mismatch", "the saved page body differs from what was written", page_id, ("source_text",))
        return None

    def _read(self, page: JSON) -> JobOrder:
        body = _body_text(self._client.list_block_children(str(page["id"])))
        return self._order(JobOrdersRow.from_page(page), page, body)

    def _order(self, row: JobOrdersRow, page: JSON, body: str | None = None) -> JobOrder:
        values: JSON = {name: getattr(row, attribute) for name, attribute in FIELDS.items()}
        for attribute in ("status", "department"):
            prop = JobOrdersRow.raw_property(page, attribute) or {}
            option = prop.get(str(prop.get("type")))
            values[attribute] = named.from_option(self._snapshot, DATABASE, attribute, option)
        return JobOrder(
            page_id=row.page_id,
            **values,
            health=_health(row.health),
            project_id=row.projects[0] if row.projects else None,
            **{name: getattr(row, attribute) for name, attribute in RELATIONS.items()},
            source_text=body,
            last_edited=page_edited(page),
        )


def _health(formula: JSON | None) -> str | None:
    """The text Notion's Health formula computes, or None."""
    value = (formula or {}).get("formula") or {}
    return str(value["string"]) if value.get("type") == "string" and value.get("string") else None


def _blocks(text: str) -> list[JSON]:
    """One paragraph block per line; a line over Notion's limit is split into several parts."""
    blocks: list[JSON] = []
    for line in text.split("\n"):
        parts = [line[i : i + BLOCK_TEXT_LIMIT] for i in range(0, len(line), BLOCK_TEXT_LIMIT)]
        blocks.append(
            {
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": part}} for part in parts]},
            }
        )
    return blocks


def _body_text(blocks: list[JSON]) -> str | None:
    """The text of a page's paragraph blocks, one line per block, or None when there are none."""
    lines = [
        codec.plain_text(block["paragraph"].get("rich_text"))
        for block in blocks
        if block.get("type") == "paragraph"
    ]
    return "\n".join(lines) if lines else None
