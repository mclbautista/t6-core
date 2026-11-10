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
- During the transition the domain `Project` carries both forms: `page_id`
  (legacy, still the equality key) and `t6_id`. References move to the T6
  Project ID in #71.
- This supersedes the page-ID-as-identity rule in #1 and #3 for Projects
  only. Other entities are still identified by their Notion page ID.
