# API read verbs for app cutovers

The API owns the Notion token. Set `T6_READ_DATA_SOURCES` to the path of a
server-side JSON object whose keys are `projects`, `contacts`, `job_orders`,
`deliverables`, `meeting_notes`, `cameras`, `studios` (read only by `t6 admin
privacy-registry`; no read verb uses it), and `external_links` (read once by
`t6 admin media migrate`; no read verb uses it); values are
Notion data source IDs. Configure only the sources the deployment uses.
`T6_NOTION_TOKEN` is also set on the API host. Neither source IDs nor Notion
filters are accepted in read requests.

Set `T6_READ_PRIVACY_REGISTRY` to a server-side JSON array of client names and
aliases, for example `["Example Client", "Example Co"]`. Keep it current: the
API can mask only names in this list. For a reader with neither the `operator`
nor the `unmasked` permission, the API replaces registered names consistently
throughout each read response. Callers with either see the original data;
`unmasked` grants nothing else, so a service such as `reports` can read real
names without the write verbs `operator` opens. The MCP server stays masked.
Reads containing names fail closed for masked callers if the registry file is not configured;
use an explicit empty array if there are no names to mask.

Build the file with `t6 admin privacy-registry`, run inside the API container
(it reads Notion directly, with the API's `T6_NOTION_TOKEN`, and
`--data-sources` defaults to `T6_READ_DATA_SOURCES`, which must name `studios`
and `projects`):

```
t6 admin privacy-registry --output /path/to/privacy-registry.json
```

The array holds each Studio name, then its aliases, then every Project name,
with spaces collapsed, blanks dropped and repeats kept once. Without `--output`
it prints the array to stdout. Only counts go to stderr. The API reads the file
at start, so restart it after a rebuild (reload without a restart is #369).

`departments` and `standing-cc` do not contain client names and do not require this file.

Inputs and outputs are in `t6.verbs` and the `t6 commands --json` catalogue;
call them with the matching methods on `t6.client.Client`. Each call returns a
standard verb outcome or `Unavailable`. Related entities use T6 IDs where a
binding exists and Notion page IDs otherwise; `page_id` always remains the
Notion page ID. A Project reference may be its T6 ID or an active or legacy
page ID. Project rotation is registry-owned and is never inferred from status.
`project list` and `project get` return the end of each date range in
`playdate_end`, `submission_deadline_end` and `archival_deadline_end` (`None`
when the date has no end; a range end is returned as Notion holds it, with its
time when it has one) and `prod_coor_supe_ids`, the people in `Prod Coor/Supe`
as T6 Person IDs where bound. `jo list`, `jo get` and `jo find` return
`project_ids`, every related Project in Notion's order, beside `project_id`,
the first.
A date with a time and no offset is returned in the `time_zone` Notion holds
for it (an aware time); with no offset and no zone it stays naive, the
workspace time. `jo list` Health is `null` when the formula gave no value and
`""` when it computed an empty string. `project_code` is the registry's value
(ADR 0001); `notion_project_code` is the code as the Notion property shows it,
and an empty registry code does not fall back to it.
Reads return the results of the Studio status and flag rules, so apps do not
evaluate these rules. `project list` and `project get` add `status_key` (the
Status without emoji and punctuation, lower case), `status_group`
(`in_progress`, `on_hold`, `incoming` or `null`), `snapshotted` (false for
Archived and Lost), `missing_roles` (the empty role attributes, in report
order; empty unless the Status is In-Progress, In-Progress TVC or Airing),
`relevant_dates` (the date attributes that matter for the Status),
`health_missing` and `health_rank` (Critical 0, At Risk 1, On Track 2,
Incoming 3, any other value 4, blank 5). `project list` also filters on
`status_group`. `jo list`, `jo get` and `jo find` add `status_group`
(`not_started`, `started`, `cancelled` or `null`), `active` and `health_flags`
(Blocked, Delayed, Urgent, ignoring emoji). `project flags` also takes `as_of`
(an ISO date, default today in Manila), `lead_warning_days` (default 30) and
`stated`, a map of `playdate`, `submission_deadline` or `archival_deadline` to
an ISO date the caller read from the PM Notes for a blank property, or `null`
for a fuzzy value. A stated date is not missing and is judged like the
property's own; `null` is never overdue. Its reply keeps `flags` and adds
`missing_roles` and `sanity`: one `{kind, first, second, first_date,
second_date, from_notes}` for each date problem (`submission_before_playdate`,
`archival_before_submission`), where `from_notes` is true when a stated date
causes it. No sentence is returned; the wording stays in the app.
`person list` filters on `registered` (has a T6 ID or not) and on `kind`
(`staff`, `freelancer`, `client` or `vendor`, the registered Person's kind; an
unknown kind is an input error, and a Contact with no T6 ID never matches).
`jo list` supports `project`, `assignee` and `status_group` (`not_started`,
`started`, `cancelled`, or `active`). `no_project` returns only the Job Orders
whose Notion page has no Project, oldest first, with an empty `project_id`; it
is the list of Job Orders raised with no Project that wait for a coordinator.
It cannot be combined with `project` (rule `input`). Meeting Notes can be filtered by Project,
inclusive `since`, exclusive `before`, exact title, title prefix and readiness;
their body is a tree of typed blocks. `external-link find` takes a typed
external reference, not a Notion filter.

The following table inventories the library/Notion reads in the referenced
cutover drafts. It also calls out writes, which do not become reads merely
because the old draft used a Notion client.

| Draft | Draft call or use | API verb |
| --- | --- | --- |
| [gateway #86](https://github.com/mclbautista/t6-sixto-gateway/pull/86) | `Contacts.list()` | `person list` |
| gateway #86 | `Contacts.get(page_id)` | `person get` |
| gateway #86 | `Contacts.set_telegram_user_id`, `Contacts.create` | Writes; outside this read ticket. |
| [watchdog #140](https://github.com/mclbautista/t6-jo-watchdog/pull/140) | `ContactReader.list()` | `person list` |
| watchdog #140 | `ProjectReader.list()` | `project list` |
| watchdog #140 | `DeliverableReader.for_project()` | `deliverable candidates` (every Deliverable page of one Project, with `page_id`, `name`, `status` and `deliverable_class`; `deliverable list` stays the registry list) |
| watchdog #140 | `get_departments()` | `departments` |
| watchdog #140 | `JobOrders.get()` | `jo get` |
| watchdog #140 | `JobOrders.find_all_by_telegram_key()` | `jo find` |
| watchdog #140 | `standing_cc()` | `standing-cc` |
| watchdog #140 | `JobOrders.create()` | `jo raise` (preview/confirm, #194). |
| [reports #116](https://github.com/mclbautista/t6-notion-reports/pull/116) | Contact data source query for names and handles | `person list` |
| reports #116 | Project data source query for snapshots | `project list` |
| reports #116 | Job Order data source query for snapshots | `jo list` |
| reports #116 | Meeting Notes queries by title, date or readiness; `list_block_children` for note bodies | `meeting-notes list` |
| reports #116 | Project data source description for leave and upcoming notices | `studio notices` |
| reports #116 | `link_timelines()` | `external-link find`, now answered from Media locations (kind `instagantt-project`); `media list` is the replacement |
| reports #116 | Meeting Notes create, append, trash and mark sent | Writes; outside this read ticket. |
| [munki #17](https://github.com/mclbautista/t6-munki-repo/pull/17) | Imports `t6.notion.writes` result types; its `Fleet.sync()` performs Bays matching and writes internally | No direct `t6.notion` read call in the draft. Fleet sync is a write, outside this read ticket. |

The registry also exposes `project get`, `project resolve`, `person find` and
`camera get` for the requested cutovers and other clients.

`person find` answers one `mention`, or a list of up to 50 `mentions` in one call
(one read of the Contacts, one result for each name in order). `suggest: true`
makes `candidates` up to 3 suggestions at 0.60 or more, even after an exact match.
`prefer_telegram: true` searches the Contacts that have a Telegram user ID first.
A request with both `mention` and `mentions`, with neither, or with more than 50
names is refused with rule `input`. See the README for the rule differences from
the apps' old matching. The table documents
what the referenced drafts actually call, including their local wrappers; it
is not a claim that their separate write cutovers have already been built.

`person list` and `person get` return the Person's kind, one of `staff`, `freelancer`,
`client` or `vendor`. A freelancer does not have the staff role.

`project resolve` takes `text`, and an optional `in_text` (default `false`).
With `in_text` true, `text` is longer text (for example `AB-101 pilot cut`) and
the verb finds the one known Project Code in it, as a whole token, case
insensitive, longest code first, with the registry rotation rule applied to that
code. No code gives `reason` `no_code_in_text`; several distinct codes give
`several_codes_in_text` with their Projects as candidates. It never falls back
to a name match. With `in_text` false the reply is as before.

### Concern brief replies

`concern for-message` accepts `chat` and `message_id` as strings and returns
`concern_ids` (an empty list for an unknown message). A brief sender can record
each Concern on a sent message with `concern record-message`, using the usual
preview and confirm flow. The reports app must call this after sending a brief;
that integration is part of the reports cutover follow-up.

For a reply, the relay selects a Concern from that lookup and calls `concern
note` with `concern`, `text`, `from_reply` in `<chat>:<brief message id>` form,
and `message_id` for the reply itself. The gateway must authenticate on behalf
of the replier. The write uses preview and confirmation; an unmapped reference
is ignored without storing the text. A message carrying several Concerns has a
separate mapping for each, and the relay must select the intended Concern.

### Concern text and the open list

A Concern holds `text` (the line the brief shows), `source` (`person`,
`pm_notes_scan`, `date_rule`, `flag_rule`, `pm_notes_read` or `leave`) and
`closed_at` (empty while open). `owner_id` may be null and `waiting_on` may be
empty. `concern list` with `open: true` returns every Concern that is not
closed, oldest first, optionally for one `project`. Each item holds `id`,
`project_id`, `owner_id`, `waiting_on`, `text`, `source`, `anchor`, `as_of`,
`opened_at`, `evidence_at`, `state`, `settles_id`, `age_seconds` and `chronic`
(open longer than 28 days). Exactly one of `raise_at_psr` and `open` must be
true, else rule `input`; the `raise_at_psr` reply is unchanged. `text`, like
`waiting_on` and `anchor`, is masked for a caller without real-name access, so a
caller that renders names needs the `unmasked` permission and the `reader` role.

### Concern observe

`concern observe` is a write that a scheduled app calls once for each run:
`run_date` (the brief's date in Asia/Manila), `findings` (`project`, `text`,
`source` of `flag_rule`, `pm_notes_read` or `leave`, optional `owner` and
`waiting_on`), `unavailable` (`project` and `source` pairs that could not be read),
`dry_run` and a required `idempotency_key`. `project` is a T6 Project ID or a
Notion page ID the registry knows. The reply has, for each Project, `open` (every
Concern that is not closed, oldest first: `id`, `text`, `source`, `opened_on`,
`age_days`, `weeks`, `chronic`, `state`, `seen_this_run`) and
`closed_since_last_run` (`id`, `text`: closed by any path since the previous
`run_date` the audit rows know, else the 7 days before), and `counts`: `new`,
`known`, `not_seen`, `unavailable`, `skipped`, `projects_with_both` and `closed`.
The run closes nothing unless the server has `T6_CONCERN_OBSERVE_CLOSES` on. The
reply names Concern text, so it is masked for a caller without real-name access.

### Concern counts

`concern counts` is read only (role `reader`) and returns counts, never text,
names or Project names. Input: optional `since` and `until` (ISO dates; the default
is the last 12 run dates) and optional `project` (a T6 Project ID or a Notion page
ID), which limits every count. The reply has `runs`, one for each `run_date`
oldest first, each with `listed`, `sent`, `new`, `closed`, `age_buckets` and
`by_source`, and for the range `closed_in_range`, `median_days_open` (days between
`opened_at` and `closed_at`) and `still_open`. A Concern is `listed` on a date when
a real `concern observe` run for it returned the Concern in `open`; it is `sent`
when `concern record-message` recorded a message for it with that `brief_date`.
`listed` starts at the first real run after the switch.

### Drive reads

`drive list`, `drive get`, `drive find`, `drive shipments`, `drive shipment` and
`drive exceptions` are read only. Every Drive they return carries a `version`, a
digest of its facts, to pass back as `expected_version` to `drive edit`. `drive
list` carries each Drive's `open_assignments`, so the inventory needs no `drive
get` per row. `drive get` shows each Assignment's `remarks`. `drive shipment` returns
the Drives' facts as they were at release and the `recipient`; for a shipment with
no kept facts it returns the current facts and `facts_at_release` is false. See
`docs/drive-identity.md` for the write side. `t6.client.APIError` carries the HTTP
status of the response as `status`, so a caller can tell a 5xx from a 4xx.
