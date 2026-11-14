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
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from t6.domain import Machine, Ring
from t6.fleet.rules import MAX_OBSERVATION_AGE, RINGS, is_stale, match_key
from t6.notion import named
from t6.notion.errors import NotionError
from t6.notion.generated import BaysRow
from t6.notion.schema import load_snapshot
from t6.notion.sync import NotionClient
from t6.notion.writes import Ambiguous, Rejected, update_row

JSON = dict[str, Any]

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


@dataclass(frozen=True, slots=True, kw_only=True)
class MachineFacts:
    """What is known about one machine. `names` are the names it goes by (hostname, computer name).

    A fact left None is unknown and is never written. `observed_at` is when the
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
        if self.last_check_in is not None and self.last_check_in.tzinfo is None:
            raise ValueError("last_check_in needs a timezone")


@dataclass(frozen=True, slots=True, kw_only=True)
class FactChange:
    fact: str
    old: Any
    new: Any


@dataclass(frozen=True, slots=True, kw_only=True)
class SyncResult:
    """What `Fleet.sync` did for one machine.

    `status` is `matched`, `unmatched` or `ambiguous`. A matched machine has the
    Bays `page_id`, the `changes` it needs, and the `skipped` facts that were
    unknown or stale. `written` is None on a dry run or when nothing changed,
    else the saved Machine, `Rejected` or `Ambiguous`.
    """

    name: str
    status: Literal["matched", "unmatched", "ambiguous"]
    page_id: str | None = None
    changes: tuple[FactChange, ...] = ()
    skipped: tuple[str, ...] = ()
    written: Machine | Rejected | Ambiguous | None = field(default=None)


def _same(old: Any, new: Any) -> bool:
    if isinstance(new, datetime) and isinstance(old, datetime) and old.tzinfo:
        return old == new.replace(second=0, microsecond=0)  # Notion drops seconds
    return bool(old == new)


class Fleet:
    """Machines in one Bays data source."""

    def __init__(self, client: NotionClient, data_source_id: str, snapshot: JSON | None = None) -> None:
        self._client = client
        self._source = data_source_id
        self._snapshot = snapshot or load_snapshot()

    def machines(self) -> tuple[Machine, ...]:
        """Every Bays row as a Machine."""
        return tuple(self._machine(page) for page in self._client.query_data_source(self._source))

    def sync(
        self, facts: Sequence[MachineFacts], *, apply: bool = False, now: datetime | None = None
    ) -> tuple[SyncResult, ...]:
        """Plan the fleet facts for each machine and, with `apply`, write them. Dry run by default."""
        now = now or datetime.now().astimezone()
        pages = self._client.query_data_source(self._source)
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

    @staticmethod
    def _rows_for(machine: MachineFacts, rows: list[tuple[JSON, BaysRow]]) -> list[tuple[JSON, BaysRow]]:
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
        stale = is_stale(machine.observed_at, now, MAX_OBSERVATION_AGE)
        changes: list[FactChange] = []
        skipped: list[str] = []
        for fact in FACTS:
            new = getattr(machine, fact)
            if new is None or (stale and fact in OBSERVED):
                skipped.append(fact)
            elif not _same(getattr(current, fact), new):
                changes.append(FactChange(fact=fact, old=getattr(current, fact), new=new))
        written = None
        if apply and changes:
            values: JSON = {change.fact: change.new for change in changes}
            if "ring" in values:
                values["ring"] = named.to_option(self._snapshot, DATABASE, "ring", values["ring"])
            if isinstance(values.get("last_check_in"), datetime):
                values["last_check_in"] = values["last_check_in"].replace(second=0, microsecond=0)
            written = self._write(row.page_id, values)
        return SyncResult(
            name=name,
            status="matched",
            page_id=row.page_id,
            changes=tuple(changes),
            skipped=tuple(skipped),
            written=written,
        )

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
