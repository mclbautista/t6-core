# Schedule snapshot: monthly calendar layout

The Client Sheet extractor (`t6.schedule`, spec #694) turns one tab of a client's calendar-style Google Sheet into a
`ScheduleSnapshot/v1`. This page describes the one layout it supports, `monthly-calendar/v1`, in words only. Fixtures are
built from this description; no client sheet, link, name or task text belongs in the repo.

## What the extractor does

`t6.schedule.extract_schedule(request)` is a pure function. The request carries the tab's cell values, merge geometry and
raw fill colours; the response is a `ScheduleSnapshot/v1`, or an unsupported-layout result. It makes no network, browser or
LLM call, writes nothing and uses no dataframe. Reading the sheet is another ticket (#722).

- Request: `ScheduleSnapshotRequest/v1`. `cells` is a list of rows (strings, numbers or null; day numbers may be floats such
  as `1.0`). `merges` use 0-based, end-exclusive bounds. `fills` is sparse. `rows_total` and `columns_total` say how big the
  tab is, so a short read shows as incomplete. `year` is optional.
- Response: `result` is `snapshot` or `unsupported_layout`. Both carry `schema_version: "ScheduleSnapshot/v1"`.
- Fixtures: `tests/fixtures/schedule/` holds versioned request and response fixtures and the JSON Schemas. A test fails when
  the committed schemas drift from the models. The planner (#695) reuses `t6.schedule.parse_response` and
  `t6.schedule.round_trip` to validate a snapshot.

## The layout

- **One tab is one dated version of the whole schedule.** A workbook may hold several tabs named like "as of November 3".
  The caller picks the tab; the extractor reads only that one.
- **Fifteen columns.** Each weekday is two columns wide, Sunday to Saturday (A:B Sunday, C:D Monday, ... M:N Saturday).
  Column O is a per-month notes column.
- **Header block.** A title spans A:N over the first rows. Under it is a colour legend of category labels, each merged across
  three columns beside a coloured swatch cell. The extractor skips this block.
- **Month blocks**, one after another down the tab. A block starts with the month name in capitals, merged across A:N, with
  "NOTES" at the top of column O. A weekday header row follows (SUNDAY to SATURDAY, each merged across its two columns). A tab
  can run across a year end.
- **Week bands.** A band starts with a day-number row: the day of the month sits in the left column of each day pair and is
  blank outside the month. The rows below, up to the next day-number row, hold that week's entries. Bands differ in height, so
  they are found by their day-number rows, never by a fixed row step. A day number is a number (`1`, `1.0`) or text of only
  digits; an entry such as "2ND pass" is text, not a day number.
- **Entries.** A single-day entry is one cell, usually merged across its day's two columns. A multi-day entry is merged
  across several day pairs, and may be several rows tall. A merge never crosses into another week band. The cell's fill
  colour matches a legend swatch; the extractor keeps it as raw evidence (`provenance.fill`) and maps it to nothing.
- **Availability lines** start with "NA:" followed by people's names. They are not tasks.
- **Notes.** Free text in column O is a month note, not a task.
- **No year anywhere.** Dates are a day number under a month name in a weekday column.

## How #717 reads it

- Each month block is a **group** (`group_key` like `m01-nov`). Groups are read independently.
- Single-day entries become **candidates**: `start` and `due` are the same ISO date. Provenance records the tab, the cell
  anchor and merge range, the raw fill, the year evidence and the date evidence (month header, day-number cell, weekday).
  `candidate_key` (`c1`, `c2`, ...) is local to the snapshot and says nothing about identity across revisions. Repeated
  titles stay separate candidates.
- "NA:" cells are listed in `non_tasks` as `availability` and are never candidates. Column O is not read.
- **Year.** The caller may pass `year`, the year of the first month block; the year rolls over when a later month number is
  lower. Without it, the extractor tries years 2020 to 2035 and uses one only when exactly one year puts every day number on
  its column's weekday. With a supplied year, a block whose weekdays do not fit is held (`weekday_mismatch`).
- **Holds.** More than one fitting year holds every readable group (`ambiguous_year`); no fitting year holds them with
  `weekday_mismatch`. A block with a bad weekday header or an unusable day number is held (`invalid_header`,
  `invalid_day_number`) and the other groups still read. A held group carries no candidates.
- **Incomplete.** Multi-day merges and entries under a blank day are not turned into candidates (#718 reads spans). They are
  reported as exceptions (`span_not_read`, `undated_entry`) and the group is `incomplete`, so a missing candidate is never
  evidence that a task is absent. A read with fewer rows or columns than `rows_total` and `columns_total` makes the whole
  snapshot `coverage.status: incomplete`.
- **Unsupported layout.** A tab with no month header merged across A:N returns `result: unsupported_layout` with a
  `diagnostic` (`empty_tab` or `no_month_header`) and no candidates.

## Coverage (#719)

- **Held and incomplete are different.** A *held* group has a problem confined to it (`invalid_header`,
  `invalid_day_number`, `ambiguous_year`, `weekday_mismatch`); it carries no candidates and the other groups still read.
  An *incomplete source* is whole-source uncertainty: `coverage.status` is `incomplete` and every group that is not
  held becomes `incomplete` with an `unread_region` exception. Held groups stay held.
- **Read coverage.** `coverage` gives `read_range` and `total_range` (A1 ranges from A1), the row and column counts, and
  `reasons`: `rows_short` or `columns_short` (fewer than the tab's reported size), `extent_unknown` (the request left out
  `rows_total` or `columns_total`) and `merges_missing` (`merges` is null). `status` is `complete` only with no reasons, so
  a short first chunk is never taken for the whole tab. With no merge list nothing is read: the snapshot has no groups and
  no candidates. An empty `merges` list means the tab has none, and it is an unsupported layout.
- **Day states.** Each group lists its day-number cells as `days`: `read` (its entries were read, so no candidate there
  means no task), `blank` (no number, outside the month), `unread` (a hold, an unread span, or an incomplete source) or
  `invalid` (not a day number). Only `read` means "no task here".
- **Absence.** `t6.schedule.coverage.standing(snapshot, group_key, day=None)` is the one question a planner asks before it
  treats a missing candidate as gone. `read` says the group (or ISO day) was read. `held`, `incomplete` and
  `not_in_snapshot` say it was not, so a candidate seen in an earlier revision is unknown, never deleted.

## Not yet read

Multi-day spans and date holds (#718), tentative markers, notes and breaks (#720), the revision
fingerprint and moved cells (#721), and the Google read and API verb (#722). The contract already has fields for them
(`tentative`, `source_id`, `source_revision`, `non_tasks`, the exception codes).
