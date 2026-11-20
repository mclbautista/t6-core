"""Machines in Bays: read them as `t6.domain.Machine` and write the fleet facts.

`Fleet.machines()` reads the Bays data source. `Fleet.sync()` matches each
machine's facts to one Bays row and writes the six fleet facts (Serial Number,
Munki Manifest, Ring, Munki Client Version, Last Check-in, OS Version) with the
typed write (`t6.notion.writes`).

Rules:
- A machine matches a row by its names against the row's Hostname and Name,
  ignoring case, spaces and ARD's `-NNN` suffix (`match_key`). A machine that
  matches no row, or more than one, or a row that two machines match, is
  skipped. A row is never created.
- It is a dry run unless `apply=True`. A fact that is unknown (None) is left
  alone, never cleared, and a fact that already holds is not written.
- The observed facts (client version, OS version, last check-in) are not
  written from a stale observation (see `is_stale`).
- A write that Notion refuses or cannot confirm is reported as the typed
  `Rejected` or `Ambiguous` result; no retry.

`Fleet.edit()` edits the other Bays properties on the same rules, limited to the
owner-approved `EDITABLE` allowlist. A property not on it, or a value of the wrong
type, fails the whole call before any row is read or written. A left-out or blank
value means no change; nothing is cleared.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import Any, Literal

from t6.domain import Machine, Ring
from t6.fleet.rules import RINGS, is_stale, match_key
from t6.notion import named
from t6.notion.errors import NotionError
from t6.notion.generated import BaysRow
from t6.notion.model import ID_KEY, NAME_KEY, TYPE_KEY
from t6.notion.schema import diff_properties, load_snapshot
from t6.notion.sync import NotionClient
from t6.notion.writes import Ambiguous, Rejected, update_row
from t6.registry import normalize_page_id

JSON = dict[str, Any]


class FleetSafetyError(ValueError):
    """Fleet operation refused because the Bays data is unsafe to use."""


DATABASE = "bays"
FACTS: tuple[str, ...] = (
    "serial_number",
    "munki_manifest",
    "ring",
    "munki_client_version",
    "last_check_in",
    "os_version",
)
"""The fleet facts t6.fleet writes, as `MachineFacts` and `Machine` attributes."""
OBSERVED: frozenset[str] = frozenset({"munki_client_version", "last_check_in", "os_version"})
"""The facts that come from observing the machine, and so go stale."""


EDITABLE: dict[str, type] = {
    "lan_ip_address": str,
    "hostname": str,
    "device_uiid": str,
    "has_paragon": bool,
    "adobe_email": str,
    "username": str,
    "avid_system_id": str,
    "avid_activation_id": str,
    "os_version": str,
    "paragon_license": str,
}
"""The Bays properties `Fleet.edit` may change (approved by Cyril on t6-core#531), as `BaysRow`
attributes with the type of value each takes. None are clearable."""


@dataclass(frozen=True, slots=True, kw_only=True)
class MachineFacts:
    """What is known about one machine. `names` are the names it goes by (hostname, computer name).

    A fact left None or blank is unknown and is never written. `observed_at` is when the
    observed facts were read; without it they count as stale.
    `last_check_in` needs a timezone.
    """

    names: tuple[str, ...]
    serial_number: str | None = None
    munki_manifest: str | None = None
    ring: Ring | str | None = None
    munki_client_version: str | None = None
    last_check_in: datetime | None = None
    os_version: str | None = None
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not any(name.strip() for name in self.names):
            raise ValueError("a machine needs at least one name")
        if self.ring is not None and self.ring not in RINGS:
            raise ValueError(f"ring must be one of {', '.join(r.value for r in RINGS)}, not {self.ring!r}")
        if self.last_check_in is not None:
            if self.last_check_in.tzinfo is None:
                raise ValueError("last_check_in needs a timezone")
            # Notion keeps minutes, so compare and write minutes.
            object.__setattr__(self, "last_check_in", self.last_check_in.replace(second=0, microsecond=0))


@dataclass(frozen=True, slots=True, kw_only=True)
class MachineEdit:
    """Edits to one machine's Bays row. `names` find the row as in `MachineFacts`; `values` are
    `EDITABLE` properties. A None or blank value means no change."""

    names: tuple[str, ...]
    values: dict[str, Any]

    def __post_init__(self) -> None:
        if not any(name.strip() for name in self.names):
            raise ValueError("a machine needs at least one name")
        unlisted = sorted(set(self.values) - set(EDITABLE))
        if unlisted:
            raise ValueError(f"not editable: {', '.join(unlisted)}; editable: {', '.join(EDITABLE)}")
        for prop, value in self.values.items():
            if value is not None and type(value) is not EDITABLE[prop]:
                raise ValueError(f"{prop} takes a {EDITABLE[prop].__name__}, not {value!r}")


@dataclass(frozen=True, slots=True, kw_only=True)
class FactChange:
    fact: str
    old: Any
    new: Any


@dataclass(frozen=True, slots=True, kw_only=True)
class EditResult:
    """What `Fleet.edit` did for one machine: `status` as in `SyncResult`, the `changes` it needs, and
    `written` (None on a dry run or when nothing changed, else the saved Machine, `Rejected` or `Ambiguous`)."""

    name: str
    status: Literal["matched", "unmatched", "ambiguous", "changed"]
    page_id: str | None = None
    changes: tuple[FactChange, ...] = ()
    written: Machine | Rejected | Ambiguous | None = field(default=None)


@dataclass(frozen=True, slots=True, kw_only=True)
class SyncResult:
    """What `Fleet.sync` did for one machine.

    `status` is `matched`, `unmatched`, `ambiguous` or `changed` (the row was
    edited, moved or archived after it was read, so nothing was written). A matched machine has the
    Bays `page_id`, the `changes` it needs, and the `skipped` facts that were
    unknown or stale. `written` is None on a dry run or when nothing changed,
    else the saved Machine, `Rejected` or `Ambiguous`.
    """

    name: str
    status: Literal["matched", "unmatched", "ambiguous", "changed"]
    page_id: str | None = None
    changes: tuple[FactChange, ...] = ()
    skipped: tuple[str, ...] = ()
    written: Machine | Rejected | Ambiguous | None = field(default=None)


def _same(old: Any, new: Any) -> bool:
    return bool(old == new)


class Fleet:
    """Machines in one Bays data source."""

    def __init__(self, client: NotionClient, data_source_id: str, snapshot: JSON | None = None) -> None:
        self._client = client
        self._source = data_source_id
        self._snapshot = snapshot or load_snapshot()

    def machines(self) -> tuple[Machine, ...]:
        """Every Bays row as a Machine; raise FleetSafetyError for a foreign page."""
        return tuple(self._machine(page) for page in self._pages())

    def _pages(self) -> list[JSON]:
        pages = self._client.query_data_source(self._source)
        for page in pages:
            parent = page.get("parent") or {}
            if parent.get("type") != "data_source_id" or normalize_page_id(
                str(parent.get("data_source_id", ""))
            ) != normalize_page_id(self._source):
                raise FleetSafetyError(f"page {page.get('id')} is not in data source {self._source}")
        return pages

    def sync(
        self, facts: Sequence[MachineFacts], *, apply: bool = False, now: datetime | None = None
    ) -> tuple[SyncResult, ...]:
        """Plan facts and optionally write them; raise FleetSafetyError for unsafe schema or pages."""
        self._check_schema(FACTS)
        now = now or datetime.now().astimezone()
        pages = self._pages()
        rows = [(page, BaysRow.from_page(page)) for page in pages]
        matches = [self._rows_for(machine, rows) for machine in facts]
        claims: dict[str, int] = {}
        for found in matches:
            for page, _ in found:
                claims[str(page["id"])] = claims.get(str(page["id"]), 0) + 1
        results = []
        for machine, found in zip(facts, matches, strict=True):
            name = machine.names[0]
            if len(found) != 1 or claims[str(found[0][0]["id"])] > 1:
                results.append(SyncResult(name=name, status="ambiguous" if found else "unmatched"))
                continue
            results.append(self._plan(name, machine, found[0], apply, now))
        return tuple(results)

    def edit(self, edits: Sequence[MachineEdit], *, apply: bool = False) -> tuple[EditResult, ...]:
        """Plan edits and optionally write them; raise FleetSafetyError for unsafe schema or pages."""
        self._check_schema(sorted({prop for edit in edits for prop in edit.values}))
        rows = [(page, BaysRow.from_page(page)) for page in self._pages()]
        matches = [self._rows_for(edit, rows) for edit in edits]
        claims: dict[str, int] = {}
        for found in matches:
            for page, _ in found:
                claims[str(page["id"])] = claims.get(str(page["id"]), 0) + 1
        results = []
        for edit, found in zip(edits, matches, strict=True):
            name = edit.names[0]
            if len(found) != 1 or claims[str(found[0][0]["id"])] > 1:
                results.append(EditResult(name=name, status="ambiguous" if found else "unmatched"))
                continue
            page, row = found[0]
            changes = tuple(
                FactChange(fact=prop, old=getattr(row, prop), new=value)
                for prop, value in edit.values.items()
                if value is not None and value != "" and not _same(getattr(row, prop), value)
            )
            written = None
            if apply and changes:
                if self._moved(page, row):
                    results.append(EditResult(name=name, status="changed", page_id=row.page_id, changes=changes))
                    continue
                written = self._write(row.page_id, {change.fact: change.new for change in changes})
            results.append(EditResult(name=name, status="matched", page_id=row.page_id, changes=changes, written=written))
        return tuple(results)

    def _check_schema(self, attributes: Sequence[str]) -> None:
        expected = [
            {"name": f.metadata[NAME_KEY], "type": f.metadata[TYPE_KEY], "id": f.metadata.get(ID_KEY)}
            for f in fields(BaysRow)
            if f.name in attributes
        ]
        live = self._client.retrieve_data_source(self._source).get("properties") or {}
        drift = diff_properties(expected, live)
        if drift["removed"] or drift["changed"]:
            raise FleetSafetyError(f"unsafe Bays schema: missing {drift['removed']}, changed {drift['changed']}")

    @staticmethod
    def _rows_for(machine: MachineFacts | MachineEdit, rows: list[tuple[JSON, BaysRow]]) -> list[tuple[JSON, BaysRow]]:
        keys = {match_key(name) for name in machine.names if name.strip()}
        return [
            (page, row)
            for page, row in rows
            if any(match_key(value) in keys for value in (row.hostname, row.name) if value)
        ]

    def _plan(
        self, name: str, machine: MachineFacts, found: tuple[JSON, BaysRow], apply: bool, now: datetime
    ) -> SyncResult:
        page, row = found
        current = self._machine(page)
        stale = is_stale(machine.observed_at, now)
        changes: list[FactChange] = []
        skipped: list[str] = []
        for fact in FACTS:
            new = getattr(machine, fact)
            if new is None or new == "" or (stale and fact in OBSERVED):
                skipped.append(fact)
            elif not _same(getattr(current, fact), new):
                changes.append(FactChange(fact=fact, old=getattr(current, fact), new=new))
        written = None
        if apply and changes:
            values: JSON = {change.fact: change.new for change in changes}
            if "ring" in values:
                values["ring"] = named.to_option(self._snapshot, DATABASE, "ring", values["ring"])
            if self._moved(page, row):
                return SyncResult(name=name, status="changed", page_id=row.page_id, changes=tuple(changes), skipped=tuple(skipped))
            written = self._write(row.page_id, values)
        return SyncResult(
            name=name,
            status="matched",
            page_id=row.page_id,
            changes=tuple(changes),
            skipped=tuple(skipped),
            written=written,
        )

    def _moved(self, queried: JSON, row: BaysRow) -> bool:
        """Whether the row was archived, or its identity or fleet facts edited, since it was queried."""
        try:
            page = self._client.retrieve_page(row.page_id)
        except NotionError:
            return True
        if page.get("archived") or page.get("in_trash") or page.get("parent") != queried.get("parent"):
            return True
        return BaysRow.from_page(page) != row

    def _write(self, page_id: str, values: JSON) -> Machine | Rejected | Ambiguous:
        saved = update_row(self._client, BaysRow, page_id, values)
        if isinstance(saved, (Rejected, Ambiguous)):
            return saved
        try:
            return self._machine(self._client.retrieve_page(page_id))
        except NotionError as error:
            return Ambiguous("readback_failed", str(error), page_id)

    def _machine(self, page: JSON) -> Machine:
        row = BaysRow.from_page(page)
        prop = BaysRow.raw_property(page, "ring") or {}
        ring = named.from_option(self._snapshot, DATABASE, "ring", prop.get(str(prop.get("type"))))
        return Machine(
            page_id=row.page_id,
            name=row.name or "",
            hostname=row.hostname,
            serial_number=row.serial_number,
            lan_ip=row.lan_ip_address,
            os_version=row.os_version,
            munki_manifest=row.munki_manifest,
            ring=ring,
            munki_client_version=row.munki_client_version,
            last_check_in=row.last_check_in,
        )
