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
