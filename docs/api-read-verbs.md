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
| watchdog #140 | `DeliverableReader.for_project()` | `deliverable list` |
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
`camera get` for the requested cutovers and other clients. The table documents
what the referenced drafts actually call, including their local wrappers; it
is not a claim that their separate write cutovers have already been built.

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
