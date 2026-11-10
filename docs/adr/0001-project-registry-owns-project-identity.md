# 1. The Project registry owns Project identity

Date: 2026-11-10. Status: accepted. Source: spec #67.

## Context

A Project used to be identified by its Notion page ID, with the Project Code
as its human key. Project Codes are reused once a Project leaves rotation, and
a Notion page can be recreated or replaced. Neither can stay the identity of a
Project over time.

## Decision

T6 keeps one authoritative **Project registry** (`t6.registry`, SQLite, one
instance for all of T6). The registry owns:

- the immutable, opaque **T6 Project ID**
- the optional Project Code and its reservation
- rotation
- the bindings between a Project and its external pages
- the append-only audit history of all of the above

Notion stays the authority for the descriptive production fields. Values the
registry owns are shown in Notion by asynchronous sync. They are displays
there, not inputs: editing them in Notion never creates or changes identity.

The registry never infers or merges identity from a code or a name. A Project
exists because someone registered it.

## Consequences

- Registry state is committed first, together with its audit entry and a sync
  intent (an outbox row). Notion is updated afterwards, so a Notion outage
  cannot lose or undo an accepted change.
- Registry routes are served on the tailnet listener only, behind their own
  read and write grants. Queue access gives no registry access.
- The domain `Project` carries both forms: `t6_id` and `page_id`. Equality and
  Episodes use the T6 ID once a Project is registered and the page ID before
  (#71). Rows that point at a Project (Job Orders, Deliverables, External
  Links, Drive Assignments, Project Start events) still hold page IDs and
  resolve through the registry's bindings, including replaced pages
  (`ProjectRefs`). The legacy form stays until every caller passes a resolver.
- This supersedes the page-ID-as-identity rule in #1 and #3 for Projects
  only. Other entities are still identified by their Notion page ID.
