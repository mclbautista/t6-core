# Domain docs

## Layout and discovery

This is a single-context repository:

- Root `CONTEXT.md` holds domain vocabulary and context.
- `docs/adr/` holds architecture decision records.

Before exploring, read `CONTEXT.md` and ADRs relevant to the work.
If `CONTEXT-MAP.md` exists, read it first and follow its pointers to relevant per-context documents and scoped ADRs.

If these files are absent, proceed silently. The domain-modeling skill creates them lazily as terms and decisions are resolved.

## Vocabulary

Use the glossary's terms in issue titles, proposals, hypotheses, and tests.
If a needed concept is absent, reconsider whether it belongs or note the gap for domain modeling.

## ADR conflicts

Identify proposals that contradict an existing ADR, cite the ADR, and explain why its decision should be reconsidered.
