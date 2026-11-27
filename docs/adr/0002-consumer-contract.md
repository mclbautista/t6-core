# 2. The consumer contract: every app uses t6-core

Date: 2026-11-13. Status: accepted; amended 2026-11-14, 2026-11-16 and 2026-11-22. Source: specs #115, #178 and #577.
Amendment of rule 10 (many Resolve agents, #777, spec #771): proposed, awaiting owner acceptance.

## Context

t6-core holds the studio's adapters for Notion, LLMs, privacy, Frame.io,
DaVinci Resolve and the fleet, but most apps do not use them yet. Several apps
carry their own Notion client, name matching, masking and LLM client. A static
study found 29 places where app code duplicates a t6-core adapter, and a
sweep found about 66 studio rules, of which about 45 were not in t6-core and
16 were copied in several repos with different values. A Notion change meant
editing several apps, and the same rule behaved differently per app.

## Decision

Every app follows these ten rules. Each has its reason.

1. **Scope.** Apps call the t6 API for Notion, LLM and Google work;
   they do not hold those credentials or import those adapters. Frame.io,
   Resolve, privacy and fleet work also goes through t6-core. Apps import
   only `t6.client` and `t6.verbs`; rules run in the API. Owner, 2026-11-16:
   "No imports via the t6 package. All of these should go through the API.
   Apps should only change in logic, not in rules." Hermes skills use the
   `t6` CLI. Read-only exploration
   by Claude through the Notion connector is exempt; any write goes through
   t6. *Why:* one service owns the credentials and outside-system changes.
2. **Boundary.** t6-core owns studio facts and rules. Each app keeps its
   workflow: prompts, ledgers, outboxes, pacing, report layout, schedules,
   allow-lists and retention. *Why:* the same rule behaves the same in every
   app, while apps stay free to differ in what is theirs.
3. **Apps use the API.** Apps use the typed API client for Notion and LLM
   work, rather than importing their adapters or using Notion property names.
   *Why:* apps depend on the studio interface, not an outside schema.
4. **Notion IDs everywhere.** The schema snapshot records property IDs and
   option IDs. Reads and writes go by ID; names are for display. Deleting and
   recreating a property, or adding one, still needs a t6-core change, and
   schema-check flags both. *Why:* a rename becomes a snapshot refresh with no
   code change.
5. **App state stays in the app.** The watchdog ledger and the gateway and
   netops outboxes remain in their apps. The VPS queue carries outside events;
   work started inside T6 uses the t6 database job table (ADR 0006). Apps
   retry API writes from their own outboxes with the same idempotency key.
   *Why:* app retry and retention choices stay local, while t6 tracks its jobs.
6. **Enforced in CI.** `t6 admin consumer-check` runs in every app and fails on
   direct calls to Notion, LLM providers or Frame.io, on any `t6` import other
   than `t6.client` and `t6.verbs` (rule `t6-import`, no exception allowed) and
   follows the API boundary; a bare mention of Frame.io in a string is not a call. The check
   cannot tell a request target from a host in test data: it flags every string
   constant that holds a provider host, because narrowing to assignments or
   non-test files would miss real calls such as a literal URL passed to a
   client. A false positive gets a `direct-http` allow-list entry. *Why:*
   the contract must hold without relying on review.
7. **Versions.** Every app pins a t6 release tag, never a commit or branch.
   Dependabot proposes bumps. The changelog flags breaking changes. A Notion
   change flows: schema-check flags it, t6-core is updated and released, apps
   bump. *Why:* upgrades are deliberate and visible.
8. **Gaps first.** A gap is filled in t6-core before an app cuts over. There
   are no local exceptions. Each gap is a t6-core ticket that blocks that
   app's cutover. *Why:* local exceptions are how the copies drifted apart.
9. **Frame.io.** Only the Frame.io worker holds the token. Apps request
   Frame.io work through t6, without holding the token; Notion, LLM and Google
   work goes through the t6 API. *Why:* one holder per credential.
10. **Resolve** stays read-only until the live project server schema is
    verified. *Why:* writes against an unverified schema can corrupt a
    project.

    Exception, 2026-11-22 (#577): T6 writes one thing in Resolve, the `t6t_`
    stamp marker on a timeline's first frame. It may add that marker, update
    its custom data and read it back. It never deletes a marker, writes
    nothing else in Resolve and never saves a project it did not have to
    change. Only the Resolve worker writes: it runs on one Mac with Resolve
    Studio and is the only holder of Resolve scripting access. It writes
    through the Resolve scripting API; the project server database stays
    read-only. Owner, 2026-11-22: stamping "should be done by code". *Why:*
    stamps typed by hand get skipped or mistyped, and nothing checks them.

    Amendment, proposed (awaiting owner acceptance; #777, spec #771): the
    exception above is widened for vfxprep, which needs markers, nested
    timelines, start timecodes and renames from any fleet Mac with Resolve
    Studio. Any fleet Resolve agent may write Resolve, only through registered
    verbs (ADR 0005), only behind a person's confirm and an automatic backtrack
    point: a backup of the project and the timeline before the write, recorded
    as a restore point. The stamp-only limit above no longer applies to the
    verbs the registry lists; the project server database stays read-only. The
    red lines stay absolute, with no exception and no confirm that lifts them:

    - Never change or delete a user marker or a user's timeline without the
      owner's confirm.
    - Never delete a Resolve project or database.
    - Never call `ArchiveProject`.
    - Never write the project server's PostgreSQL directly.

    Every write records the host, its Resolve version and the restore point.
    *Why:* the studio needs more than one write and more than one Mac, and a
    confirm plus a restore point keeps each write reversible.

## Consequences

- t6.notion write functions read back and return the saved domain object, or
  a typed failure that says whether the write may have happened (ambiguous).
  Apps keep idempotency keys and retry decisions.
- Credential withholding applies to every free-text field and page body
  t6-core writes to Notion, and to every LLM input.
- Apps move onto the API one rule at a time. Once a verb answers a rule, the app
  calls it and drops its own copy; the API holds Notion, matching, privacy and LLM
  work (spec #426).
- Out of scope here: the app cutovers themselves, which are tracked in each
  app's spec.

## Superseded

The original wording of rules 1, 3, 5 and 9 is retained here for history;
the amended rules above and ADRs 0003 and 0006 take precedence.

1. **Scope.** All repo code that touches Notion, LLMs, privacy, Frame.io,
   Resolve or the fleet goes through t6-core. Hermes skills use the `t6` CLI.
   Read-only exploration by Claude through the Notion connector is exempt; any
   write goes through t6. *Why:* one place to change when an outside system
   changes.
3. **Apps use `t6.domain`.** No app imports `t6.notion.generated` or uses a
   Notion property name. *Why:* apps then do not break when Notion changes.
5. **App state stays in the app.** The watchdog ledger and the gateway and
   netops outboxes are not moved into t6-core. The t6-core queue is for
   webhook jobs only. *Why:* app state follows the app's own retry and
   retention choices.
9. **Frame.io.** Only the Frame.io worker holds the token. It serves a small
   read API on the LAN and tailnet, and a token-free `t6.frameio` client calls
   it. *Why:* one holder of the credential.
