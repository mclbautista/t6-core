"""ScheduleSnapshot/v1: what the Client Sheet extractor takes in and hands the planner.

The request carries one tab as plain values plus merge geometry. The response
is either a snapshot or a typed unsupported-layout result. Nothing here is a
dataframe, and nothing here names Instagantt. `parse_response` and
`round_trip` validate a payload the way the planner should.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, field_validator, model_validator

SCHEMA_VERSION: Final = "ScheduleSnapshot/v1"
REQUEST_SCHEMA_VERSION: Final = "ScheduleSnapshotRequest/v1"
LAYOUT_VERSION: Final = "monthly-calendar/v1"

CellValue = str | int | float | None
IsoDate = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]
GroupStatus = Literal["complete", "held", "incomplete"]
CoverageReason = Literal["rows_short", "columns_short", "extent_unknown", "merges_missing"]
DayStateKind = Literal["read", "blank", "unread", "invalid"]

# A hold or an unread part of a group. The first four are raised by #717; the rest are
# defined for the tickets that detect them (#718 spans and year, #719 coverage).
ExceptionCode = Literal[
    "invalid_header",
    "invalid_day_number",
    "ambiguous_year",
    "weekday_mismatch",
    "ambiguous_span",
    "span_not_read",
    "undated_entry",
    "unread_region",
]
EvidenceKind = Literal["year", "weekday_alignment", "header"]
NonTaskKind = Literal["availability", "note", "break"]
UnsupportedCode = Literal["empty_tab", "no_month_header"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceScope(_Model):
    """Document and tab identity. Opaque strings the caller chose; the extractor never opens them."""

    document_id: str = Field(min_length=1)
    tab_id: str = Field(min_length=1)
    tab_title: str | None = None


class Merge(_Model):
    """A merged range, 0-based, end exclusive (the Sheets grid-range convention)."""

    start_row: int = Field(ge=0)
    end_row: int = Field(ge=1)
    start_col: int = Field(ge=0)
    end_col: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> Merge:
        if self.end_row <= self.start_row or self.end_col <= self.start_col:
            raise ValueError("merge must cover at least one cell")
        return self


class CellFill(_Model):
    """A cell's raw background colour, kept as evidence. Mapping it to a category is out of scope."""

    row: int = Field(ge=0)
    col: int = Field(ge=0)
    color: str = Field(min_length=1)


class ScheduleRequest(_Model):
    """One tab read in full. `rows_total` and `columns_total` are the tab's own size, when known."""

    schema_version: Literal["ScheduleSnapshotRequest/v1"] = REQUEST_SCHEMA_VERSION
    source_scope: SourceScope
    source_revision: str | None = None
    cells: list[list[CellValue]]
    merges: list[Merge] | None = None  # None: no merge list was read. An empty list: the tab has no merges.
    fills: list[CellFill] = Field(default_factory=list)
    rows_total: int | None = Field(default=None, ge=0)
    columns_total: int | None = Field(default=None, ge=0)
    year: int | None = Field(default=None, ge=1900, le=2200)


class Coverage(_Model):
    """How much of the tab was read, against its full extent. Incomplete is never evidence of absence.

    `read_range` and `total_range` are A1 ranges from A1 (`total_range` is null when the tab's size was not
    reported). `status` is complete exactly when `reasons` is empty.
    """

    status: Literal["complete", "incomplete"]
    rows_read: int = Field(ge=0)
    columns_read: int = Field(ge=0)
    rows_total: int | None = None
    columns_total: int | None = None
    read_range: str | None = None
    total_range: str | None = None
    reasons: list[CoverageReason] = Field(default_factory=list)

    @model_validator(mode="after")
    def _status_matches_reasons(self) -> Coverage:
        if (self.status == "complete") == bool(self.reasons):
            raise ValueError("coverage is complete exactly when it has no reasons")
        return self


class Evidence(_Model):
    kind: EvidenceKind
    detail: str
    anchor: str | None = None


class GroupException(_Model):
    code: ExceptionCode
    detail: str
    anchor: str | None = None


class DayState(_Model):
    """What was read at one day-number cell. Only `read` lets an empty day mean "no task here".

    `blank` is a day-number cell with no number (outside the month), `unread` a day whose entries were not read
    (a hold, an unread span or an incomplete source), `invalid` a day-number cell that is not a day.
    """

    anchor: str
    state: DayStateKind
    day: int | None = None
    date: IsoDate | None = None


class Group(_Model):
    """A larger part of the tab, processed on its own: one month block."""

    group_key: str = Field(min_length=1)
    label: str
    status: GroupStatus
    anchor: str
    year: int | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    exceptions: list[GroupException] = Field(default_factory=list)
    days: list[DayState] = Field(default_factory=list)

    @model_validator(mode="after")
    def _status_matches_exceptions(self) -> Group:
        if (self.status == "complete") == bool(self.exceptions):
            raise ValueError("a group is complete exactly when it has no exceptions")
        return self


class YearEvidence(_Model):
    year: int
    method: Literal["supplied", "inferred_weekday_alignment"]


class DateEvidence(_Model):
    month: int = Field(ge=1, le=12)
    day: int = Field(ge=1, le=31)
    weekday: str
    month_header_anchor: str
    day_number_anchor: str


class Provenance(_Model):
    tab_id: str
    tab_title: str | None = None
    anchor: str
    range: str
    merged: bool
    fill: str | None = None
    year_evidence: YearEvidence
    date_evidence: DateEvidence


class Candidate(_Model):
    """A task the sheet shows. `candidate_key` is local to this snapshot, never an identity across revisions."""

    candidate_key: str = Field(min_length=1)
    group_key: str = Field(min_length=1)
    title: str = Field(min_length=1)
    start: IsoDate
    due: IsoDate
    tentative: str | None = None
    source_id: str | None = None
    provenance: Provenance

    @field_validator("start", "due")
    @classmethod
    def _real_date(cls, value: str) -> str:
        date.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def _ordered(self) -> Candidate:
        if self.start > self.due:
            raise ValueError("start must not be after due")
        return self


class NonTask(_Model):
    """Text that is not a task: availability, a note or a break. Never executed, never a candidate."""

    kind: NonTaskKind
    text: str
    group_key: str
    anchor: str


class ScheduleSnapshot(_Model):
    schema_version: Literal["ScheduleSnapshot/v1"] = SCHEMA_VERSION
    result: Literal["snapshot"] = "snapshot"
    source_scope: SourceScope
    source_revision: str | None = None
    layout_version: Literal["monthly-calendar/v1"] = LAYOUT_VERSION
    coverage: Coverage
    groups: list[Group]
    candidates: list[Candidate] = Field(default_factory=list)
    non_tasks: list[NonTask] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> ScheduleSnapshot:
        keys = [g.group_key for g in self.groups]
        if len(set(keys)) != len(keys):
            raise ValueError("group keys must be unique")
        held = {g.group_key for g in self.groups if g.status == "held"}
        candidate_keys = [c.candidate_key for c in self.candidates]
        if len(set(candidate_keys)) != len(candidate_keys):
            raise ValueError("candidate keys must be unique")
        for candidate in self.candidates:
            if candidate.group_key not in keys:
                raise ValueError(f"candidate {candidate.candidate_key} names an unknown group")
            if candidate.group_key in held:
                raise ValueError(f"held group {candidate.group_key} must not carry candidates")
        return self


class Diagnostic(_Model):
    code: UnsupportedCode
    message: str


class UnsupportedLayout(_Model):
    """The tab is not the supported layout. No candidates, and no guess."""

    schema_version: Literal["ScheduleSnapshot/v1"] = SCHEMA_VERSION
    result: Literal["unsupported_layout"] = "unsupported_layout"
    source_scope: SourceScope
    source_revision: str | None = None
    diagnostic: Diagnostic


ScheduleResult = Annotated[ScheduleSnapshot | UnsupportedLayout, Field(discriminator="result")]

_REQUEST: TypeAdapter[ScheduleRequest] = TypeAdapter(ScheduleRequest)
_RESULT: TypeAdapter[ScheduleSnapshot | UnsupportedLayout] = TypeAdapter(ScheduleResult)


def _load(payload: str | bytes | Mapping[str, Any]) -> Any:
    return json.loads(payload) if isinstance(payload, (str, bytes)) else payload


def parse_request(payload: str | bytes | Mapping[str, Any]) -> ScheduleRequest:
    """Validate a request payload. Raises `pydantic.ValidationError`."""
    return _REQUEST.validate_python(_load(payload))


def parse_response(payload: str | bytes | Mapping[str, Any]) -> ScheduleSnapshot | UnsupportedLayout:
    """Validate a response payload, as the planner should. Raises `pydantic.ValidationError`."""
    return _RESULT.validate_python(_load(payload))


def dump_response(result: ScheduleSnapshot | UnsupportedLayout) -> dict[str, Any]:
    """The wire form: plain JSON values."""
    dumped: dict[str, Any] = _RESULT.dump_python(result, mode="json")
    return dumped


def round_trip(result: ScheduleSnapshot | UnsupportedLayout) -> ScheduleSnapshot | UnsupportedLayout:
    """Serialize, validate and parse back. Raises `ValueError` unless the copy equals the original."""
    again = parse_response(json.dumps(dump_response(result)))
    if again != result:
        raise ValueError("response did not survive a round trip")
    return again


def response_json_schema() -> dict[str, Any]:
    return _RESULT.json_schema()


def request_json_schema() -> dict[str, Any]:
    return _REQUEST.json_schema()
