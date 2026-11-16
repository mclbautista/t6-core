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

## Exception, 2026-11-16: requests to the Frame.io worker

Requests to the Frame.io worker (job kind `frameio.request`) travel on the `frameio`
queue of the VPS queue server. The main worker has used it since #26, and the API
since #275 (Project Start). They start inside T6, so this is an exception to the
decision above.

The owner accepted the exception on 2026-11-16. The reason is ADR 0003: the Frame.io
worker is the only holder of the Frame.io token and never opens the t6 database, so
it cannot read a job table there. No other work that starts inside T6 may use the
VPS queue without a new note here.

Note, 2026-11-16 (#398): the Frame.io review timer (`POST /review/timers`, then the `frameio.review_done` job on `main`) is part of this exception. The timers live in the VPS queue file, so the edge also serves that one route and the worker on the VPS `main` queue runs `frameio.review_done`. The edge still answers 404 to everything else.
