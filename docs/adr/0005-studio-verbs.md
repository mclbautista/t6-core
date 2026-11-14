# 5. Studio verbs

Date: 2026-11-14. Status: accepted. Source: spec #178.

## Context

Today's commands have unrelated shapes, and callers cannot rely on one typed
contract for studio rules, safe writes and future tool discovery.

## Decision

Commands use `t6 <noun> <verb>`. Writes use studio verbs, with no generic
`update` or `delete`; common reads are `list`, `get`, `find` and `activity`.
A declarative verb registry describes each verb for the CLI, JSON command
catalogue and read-only MCP server.

Every verb returns one of **Done | Preview | NeedsPerson | Refused(rule) |
Ambiguous**. Every write is previewed before confirmation. Preview returns a
signed, single-use token; confirm sends that token back. When a preview needs
work only a worker can do, the call returns a job ID and the job result is the
Preview with its token. Tokens expire and refuse changed input. Every write is
idempotent by key, so callers can retry safely.

The actor is the authenticated Person or named service, never a flag supplied
by the caller. Each API secret is bound to one Person or named service; only
the gateway may act on behalf of a Telegram ID. A role-to-verb table decides
authorization.

Every verb has Pydantic input and output models; JSON schemas are generated
from those models. Keep the hand-written standard-library HTTP server for the
API: the typed models and schemas do not require a server framework, and
retaining the existing server avoids adding one solely for this contract.

## Consequences

- Callers must handle all five outcomes and preserve idempotency keys across
  retries. A preview alone does not perform the write.
- The server must enforce authentication, authorization, token and idempotency
  rules; keeping it does not remove those responsibilities.
- Adding a verb includes its models and registry entry, keeping command
  discovery and the API contract aligned.
