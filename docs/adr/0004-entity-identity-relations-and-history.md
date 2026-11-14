# 4. T6 owns entity identity, relations and history

Date: 2026-11-14. Status: accepted. Source: spec #178.

## Context

ADR 0001 made the Project registry authoritative for Project identity, but
other entities still use Notion page IDs. A replaced page can break references,
and Notion alone does not provide the studio's durable relations and history.

## Decision

T6 owns identity, relations and append-only history for every entity. Each
entity has a prefixed, opaque t6 ID. Notion page IDs are external bindings,
not identity: an active binding points to the current page and a legacy binding
continues to resolve a replaced page to the same entity. This extends ADR 0001
beyond Projects, superseding its statement that other entities are identified
by Notion page ID.

Notion owns human-typed fields. T6-owned values shown in Notion are
projections in two classes:

- **Identity fields**, such as T6 ID, Project Code and Rotation: an edit in
  Notion is put back to the t6-owned value.
- **State fields**, such as JO Status, Project Status and Health: an edit in
  Notion is accepted as if the matching studio verb ran and logged in Activity
  as "set in Notion by X". An edit that breaks a rule is flagged, never
  silently reverted.

T6 keeps the last-seen value of each watched property: Notion's webhook names
the changed property but does not supply its old value.

## Consequences

- Recreating a page does not create a new identity or erase its relations and
  history; bindings must be maintained explicitly.
- Notion webhooks must be set up and proven for every watched data source.
  Reconciliation must distinguish identity edits from state edits and surface
  rule-breaking changes for a person.
- T6 commits its own changes and sync intent before projecting them to Notion,
  preserving ADR 0001's commit-first boundary.
