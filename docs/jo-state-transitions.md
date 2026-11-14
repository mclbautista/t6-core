# Job Order state transitions

The status names are the Notion JO Status values. New JOs default to Needs
Intake Review unless raised with a status; migration retains the observed status.
Blocked and delayed are T6 state alongside the status, rather than Notion Status
options. Each confirmed transition queues one Status projection, including a
block, unblock or delay whose Status value remains unchanged.

| Verb | Allowed current Status | New Status | Additional rule |
| --- | --- | --- | --- |
| submit | Needs Intake Review, Pending | In progress | Must not be blocked |
| close | In progress, Needs Completion Review | Done | Must not be blocked |
| block | In progress, Needs Completion Review | unchanged | Not already blocked; reason required |
| unblock | In progress, Needs Completion Review | unchanged | Must be blocked |
| delay | Needs Intake Review, Pending, In progress, Needs Completion Review | unchanged | ISO date and reason required |
| cancel | Needs Intake Review, Pending, In progress, Needs Completion Review | Cancelled | Reason required; clears block |

All other combinations are refused. Done and Cancelled are terminal. Delay
records the latest date and reason; a later delay replaces them. A preview
must be confirmed against the same state it observed. Activity and audit retain
all confirmed transitions and their inputs.

## Edits in Notion

The main worker watches changed property IDs on bound Projects and Job Orders.
It records the last observed or projected values in the t6 database. Identity
edits are restored; status edits use the corresponding verb and its permission
and transition rules. A refused edit stays in Notion and appears in the
registry review list and Project Activity. Outstanding JO projections do not
overwrite a flagged status. Project Health changes are flagged for review until
there is a corresponding Health verb. Project hold and JO cancel require a
reason, which a status edit alone does not provide, so those edits are flagged.

A webhook author is resolved using a `notion_user` binding to a Person. The
registry's `bind_entity('notion_user', notion_user_id, person_id, actor=...)`
method creates the binding; `person_id` is a `T6Id` of kind `PERSON`. Bindings
should be made only after verifying the Notion user and Person represent the
same person. Notion UUID spellings are normalized. A missing binding, inactive
Person, or missing verb permission is flagged for review, with the Notion user
name in Activity. The main worker needs access to Notion's user endpoint to
obtain names when webhook authors contain only IDs.
