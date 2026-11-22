"""Bay scan reports: what a bay's local Resolve disk library held when it was last scanned (ADR 0008, #376).

The scan script (`t6.bay_scanner`) prints one report per bay. The Mac mini posts it, and t6 ties it to a Bay (`t6b_`) through
the machine serial and the Machine to Bays mapping in `t6.fleet`, never by name. t6 keeps the latest report per Bay and only reads
it back; nothing here changes a bay, Resolve or Notion.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from t6.registry.core import normalize_page_id
from t6.registry.sqlite import SqliteProjectRegistry, _audit
if TYPE_CHECKING:  # `t6.verbs` imports the report model, so it is imported inside the methods that need it
    from t6.verbs import Outcome

FORMAT_VERSION = 1
"""The report format this t6 reads. The scan script prints the same number; a report with another number is refused."""

STALE_AFTER_SECONDS = 3 * 24 * 3600
"""A report whose scan is older than this shows as stale (ADR 0008 decision 5)."""


class LocalCopy(BaseModel):
    """One Resolve project held in the bay's local disk library."""

    model_config = ConfigDict(extra="forbid")
    project_uuid: str = Field(min_length=1, description="`SM_Project.SM_Project_id`.")
    project_name: str = Field(description="`SM_Project.ProjectName`.")
    folder_path: str = Field(description="The folders above the project below `Projects/`, joined by `/`; empty at the top level.")
    last_modified: int = Field(ge=0, description="`SM_Project.LastModTimeInSecs`, unix seconds.")
    timeline_count: int = Field(ge=0, description="The number of rows in `Sm2Timeline`.")


class ScanProblem(BaseModel):
    """A project that could not be read, with the reason. It is never guessed at."""

    model_config = ConfigDict(extra="forbid")
    folder_path: str
    project: str
    reason: str


class BayScanReport(BaseModel):
    """What the scan script prints for one bay. `format_version` is checked by the ingest, so a wrong one gets a clear refusal."""

    model_config = ConfigDict(extra="forbid")
    format_version: int
    serial: str = Field(min_length=1, description="The bay's `IOPlatformSerialNumber`.")
    scanner_version: str = Field(min_length=1)
    scanned_at: AwareDatetime
    resolve_version: str | None = Field(default=None, description="The installed Resolve version, when the bay has it.")
    status: Literal["ok", "skipped_resolve_open"]
    local_copies: list[LocalCopy]
    errors: list[ScanProblem] = Field(default_factory=list)

    @model_validator(mode="after")
    def _skipped_scans_nothing(self) -> "BayScanReport":
        if self.status == "skipped_resolve_open" and (self.local_copies or self.errors):
            raise ValueError("a skipped scan lists no local copies and no errors")
        return self


def bay_for_serial(serial: str, machines: Iterable[Any], bound: dict[str, str]) -> tuple[str | None, str]:
    """The Bay a serial belongs to, or why none does.

    `machines` are the Bays rows read as Machines (serial and page ID); `bound` maps a Notion page ID to its registered Bay.
    Only an exact serial (case and edge spaces aside) matches: a name is never used.
    """
    wanted = serial.strip().casefold()
    rows = [machine for machine in machines if (machine.serial_number or "").strip().casefold() == wanted]
    if not rows:
        return None, "No Bays row has this serial"
    if len(rows) > 1:
        return None, "More than one Bays row has this serial"
    bay = bound.get(normalize_page_id(rows[0].page_id))
    return (bay, "") if bay else (None, "The Bays row for this serial has no registered Bay")


class BayScans:
    def __init__(self, registry: SqliteProjectRegistry) -> None:
        self.registry = registry

    def bound_pages(self) -> dict[str, str]:
        """Each Bay's active Notion page ID mapped to the Bay."""
        with self.registry._transaction() as db:
            rows = db.execute("SELECT bindings.page_id, bays.id FROM bays JOIN bindings ON bindings.project_id = bays.id "
                              "AND bindings.system = 'notion' AND bindings.active = 1").fetchall()
        return {normalize_page_id(row[0]): row[1] for row in rows}

    def ingest(self, report: BayScanReport, bay_id: str, *, actor: str) -> Outcome:
        """Store the report as the Bay's latest. The same or an older scan changes nothing, so a repeat post is safe."""
        from t6.verbs import Done, Refused
        scanned = report.scanned_at.timestamp()
        body = json.dumps(report.model_dump(mode="json"), sort_keys=True)
        with self.registry._transaction() as db:
            if not db.execute("SELECT 1 FROM bays WHERE id = ?", (bay_id,)).fetchone():
                return Refused(rule="no_bay", reason="The Bay is not registered")
            row = db.execute("SELECT scanned_at FROM bay_scan_reports WHERE bay_id = ?", (bay_id,)).fetchone()
            if row and row[0] >= scanned:
                return Done(result={"bay_id": bay_id, "stored": False, "reason": "unchanged" if row[0] == scanned else "older than the stored report"})
            now = self.registry._clock()
            db.execute("INSERT INTO bay_scan_reports (bay_id, serial, scanned_at, received_at, report) VALUES (?, ?, ?, ?, ?) "
                       "ON CONFLICT(bay_id) DO UPDATE SET serial = excluded.serial, scanned_at = excluded.scanned_at, "
                       "received_at = excluded.received_at, report = excluded.report", (bay_id, report.serial.strip(), scanned, now, body))
            _audit(db, bay_id, "bay_scan_report", actor, now, None if row is None else {"scanned_at": row[0]},
                   {"scanned_at": scanned, "status": report.status, "local_copies": len(report.local_copies), "errors": len(report.errors)},
                   summary="bay scan report")
        return Done(result={"bay_id": bay_id, "stored": True, "reason": "newer"})

    def list(self, bay: str | None = None) -> Outcome:
        """Each Bay's last report and whether it is stale; a Bay with no report is listed as `no_report`, never as having no copies."""
        from t6.verbs import Done, Refused
        now = self.registry._clock()
        with self.registry._transaction() as db:
            rows = db.execute(
                "SELECT bays.id, bindings.page_id, r.serial, r.scanned_at, r.received_at, r.report FROM bays "
                "LEFT JOIN bindings ON bindings.project_id = bays.id AND bindings.system = 'notion' AND bindings.active = 1 "
                "LEFT JOIN bay_scan_reports r ON r.bay_id = bays.id ORDER BY bays.id").fetchall()
        wanted = None if bay is None else normalize_page_id(bay)
        if wanted is not None:
            rows = [row for row in rows if row[0] == bay or (row[1] is not None and normalize_page_id(row[1]) == wanted)]
            if not rows:
                return Refused(rule="bay", reason="Bay not found")
        listed = []
        for bay_id, page_id, serial, scanned_at, received_at, body in rows:
            entry: dict[str, Any] = {"bay_id": bay_id, "page_id": page_id, "state": "no_report"}
            if body is not None:
                report = json.loads(body)
                entry.update(state="stale" if now - scanned_at > STALE_AFTER_SECONDS else "fresh", serial=serial,
                             scanned_at=_iso(scanned_at), received_at=_iso(received_at), scanner_version=report["scanner_version"],
                             resolve_version=report["resolve_version"], status=report["status"], local_copies=report["local_copies"],
                             errors=report["errors"])
            listed.append(entry)
        return Done(result={"bays": listed})


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
