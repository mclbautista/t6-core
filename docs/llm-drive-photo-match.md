# Drive photo match

Ticket #727, parent #693. `t6.llm.drive_photo_match.PHOTO_MATCH`
(`drive-photo-match-v1`) replaces the label-read task in Capture resolution.
Jev runs first, unchanged. Only the configured recording chat and topic may
reach this task, under [ADR 0009](adr/0009-recording-channel.md).

Each Item with a nonempty shortlist sends one request: the post photo bounded
to 1600 px on its long side, up to six shortlisted Drives labelled A–F, the
Item and caption as withheld text, and available Reference photos bounded to
320 px. Images are oriented and re-encoded as JPEG without source metadata.
Missing or unreadable Reference photos leave the Drive as a text candidate.
No surrounding conversation is sent.

The reply is JSON with `guesses` (at most three distinct shortlisted letters,
finite confidence in 0–1, and a nonempty one-line reason) and `label_text`
(visible text or null). Guesses are ranked by confidence after preserving
eligible 0.95 exact matches first. Visible label text can trigger the same
confirmed, unique, direction-filtered Reference-photo rule as the Item text.
Guess one creates the to-review Movement; every Item waits for Approval.

A missing provider, error, timeout, invalid reply, invalid post image or empty
shortlist keeps the Capture and creates no guess or Movement. Text-only posts
never call vision. Provider calls run outside the database write transaction;
an undo, edit or Reload during a call cannot restore stale results.

Install `t6[llm]` for image resizing. Configure `llm.tasks.drive-photo-match-v1` (or `llm.default`) in
`T6_LLM_CONFIG`. The provider must declare `supports_images: true` and
`zdr_enforced` retention. No provider configuration still resolves Items into
the Inbox. The debug topic shows shortlist letters and Drives, thumbnails
sent, guesses with reasons, exact eligibility checks, model, latency, token
usage and provider-reported USD cost. Unreported cost is shown as unknown.

## Eval cases

- **Task:** Compare the new photo with shortlisted Reference photos and rank
  the Drives shown for the Item.
- **Expected properties:** Only shortlisted letters, at most three distinct
  guesses, finite confidence in 0–1, a nonempty one-line reason and visible
  label text only. Must-pass cases rank the matching foreground box first,
  avoid an unrelated background Drive as guess one, and return no guess for
  an empty or unrelated photo.
- **Graders:** `grade_match` checks schema, shortlist membership, rank one and
  visible label text against labelled ground truth. People review visual
  grounding, reasons and confidence. No model judge decides pass or fail.
- **Synthetic cases:** `t6.llm.drive_photo_match_evals.CASES` and
  `synthetic_photo` describe and generate invented prop boxes and tape labels
  at runtime. HTTP verb tests use generated Reference photos and scripted
  fake-provider replies, and also check that the grader rejects wrong
  outputs. CI runs these offline tests; they verify the contract, not model
  accuracy.
- **Real cases:** The spec identifies private TRANSMITTAL cases. They and live
  run records stay off-repo; none are read or committed by this ticket.

A recorded live run with human review blocks release under
[LLM task rules](agents/llm-task-specs.md). Select the cheapest image-capable
model that reaches the spec's target of at least 50% correct guess one on the
real cases. Offline fake-provider results are not evidence of that accuracy.
