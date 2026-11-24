# 10. Drive cutover is a one-way config setting

Date: 2026-11-24. Status: accepted (owner, 2026-11-24).
Source: [spec #615](https://github.com/mclbautista/t6-core/issues/615), web pages grill
(decisions 51 and 56 to 58 in the project's grill record).
Replaces the `drive cutover` verb described in docs/deployment.md for t6 0.6.26.

## Context

Until the cutover on 2026-12-03, Drive data is reloaded from the custodian's two
Google Sheets every night, and recordings are Practice records. The first design
switched to live with an operator verb, `drive cutover`, that previewed and ran a
final Reload, then left Practice mode. On 2026-11-24 a sheet read run inside the live
API stalled every service on Sixto for about 8 minutes. The owner wants going live to
be a reviewed config change read on deploy, with no data replacement as a side effect
of starting a process.

## Decision

- Going live is one deployment setting, **`T6_DRIVE_LIVE=true`**, read by the API and
  the main worker when they start.
- Starting with the setting on turns Practice mode off, stops the nightly Reload, and
  refuses every Reload (the verb, the nightly job and the "Reload now" button) with
  rule `cutover`. **Nothing is reloaded at startup.** The last Reload before the deploy
  (the 00:00 Manila nightly on the cutover day, or a "Reload now" just before) is the
  final load.
- **One way only.** The first start with the setting on records live mode in the
  database. If the setting is later removed or set false, t6 stays live and logs a
  warning; it never returns to Practice mode, because the next Reload would replace
  real records.
- The `drive cutover` verb is removed. There is exactly one way to go live.
- Making the two source sheets read-only stays a manual step for the custodian; the
  Sheets adapter has read-only scope.

## Consequences

- Cutover day steps: set `T6_DRIVE_LIVE=true` in the deployment env file (netops),
  deploy with the owner's approval, check that Reloads are refused and replies no
  longer say "Practice", then the custodian makes both sheets read-only.
- Until the implementing ticket ships, the deployed 0.6.26 still has `drive cutover`
  and no `T6_DRIVE_LIVE`. Agents must not run `drive cutover`; the cutover waits for
  the release that implements this ADR (planned 0.6.27).
- Tests must cover: startup with the setting on reloads nothing; Reload refused with
  rule `cutover`; live mode survives removal of the setting; the verb is gone.
