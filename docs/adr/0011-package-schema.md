# 11. Package schema: checklist kinds, gates and versioned specs

Date: 2026-11-27. Status: proposed (awaiting owner acceptance).
Source: [#778](https://github.com/mclbautista/t6-core/issues/778), spec #771 (vfxprep).

## Context

Packages already carry a checklist from a Package Spec, with advisory and
blocking items (`docs/package-specs-schema.md`). vfxprep adds Package kinds
such as `vfx_prep` whose checklist mixes preparation, the work itself and the
handoff to the next person. One basic schema should serve every Package kind,
so a new kind is a new spec and not new code.

## Decision

1. **Checklist item.** Every item has `text`, `kind`, `blocking`, `checked_by`
   and `checked_at`, and may have `evidence` (a link) and `automated_by` (the
   verb or job that may check it). `kind` is one of:
   - `mise`: preparation that must be in place before the work starts.
   - `pipeline`: the work steps, run by verbs.
   - `handoff`: what must hold before the Package goes to the next person.
2. **Gates.** Open blocking `mise` items stop the pipeline verbs. Open blocking
   `pipeline` items stop the handoff. Verbs enforce both gates and refuse with
   the rule and the open items. An open advisory item never stops anything.
3. **Versioned Package Specs.** Each Package kind (`delivery`, `vfx_prep`, ...)
   has a versioned Package Spec: a Notion page the team may edit. The spec is
   snapshotted into T6 when a Package is created; later edits affect only new
   Packages.
4. **Changes to the schema.** The item fields, the kinds and the gates change
   only by amending this ADR. A Package Spec may change its items, not the
   schema.

*Why:* one schema keeps every kind readable by the same verbs. Gates stop
work on a missing prerequisite. The snapshot means a Package keeps the
checklist it started with, and the team can still edit specs.

## Consequences

- The Package model carries `kind` on checklist items, and the verbs enforce
  the gates. Both are separate tickets under spec #771; this ADR changes no
  code.
- Kasado (CONTEXT) keeps its rule: every blocking item ticked. How it reads
  the new kinds, and how existing Packages (items with no kind) are read, is
  open (see the PR).
