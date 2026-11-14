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
