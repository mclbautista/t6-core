# 6. Two queues

Date: 2026-11-14. Status: accepted. Source: spec #178.

## Context

ADR 0002 rule 5 reserved the t6-core queue for webhook jobs. The studio API
also needs to track work started within T6, including worker-backed previews,
without treating it as an outside event.

## Decision

The VPS queue carries only events arriving from outside T6, such as webhooks
and OAuth callbacks. Work that starts inside T6 uses a job table in the t6
database. This supersedes ADR 0002's original rule 5 on the queue; app-owned
ledgers and outboxes remain in their apps.

## Consequences

- Outside-event intake and internal jobs have distinct responsibilities.
- Workers use the internal job table for T6-started work; apps retain their
  own retry and retention choices for app-owned state.

## Note, 2026-11-15: requests to the Frame.io worker

Requests to the Frame.io worker (job kind `frameio.request`) travel on the `frameio`
queue of the VPS queue server. The main worker has used it since #26, and the API
since #275 (Project Start confirm). They start inside T6, so this is an exception
to the decision above. It waits for the owner's decision: keep it, or move these
requests to a job table. Nothing changes until the owner decides.
