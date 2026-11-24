# 9. TRANSMITTAL is a recording channel

Date: 2026-11-23. Status: accepted (owner, 2026-11-23).
Source: [spec #615](https://github.com/mclbautista/t6-core/issues/615).
Amends [ADR 0007](0007-no-chat-text.md) for one configured chat.

## Context

Drive movements are recorded by posting a photo and one text line in
TRANSMITTAL. Treating that input as ordinary linked-chat conversation would
prevent recording it under ADR 0007. Opening all linked chats to retention or
LLM processing would expose unrelated conversation.

## Decision

TRANSMITTAL is a named **recording channel**. Its drive posts (a photo plus
one text line) are input to `drive capture`, just as a command addressed to
Sixto is input to a verb under ADR 0007. Capture keeps the post before any
reading or matching. The photo may go to the LLM only to read Drive labels
and serials; this does not authorize sending the surrounding conversation.
Photo retention follows the provider retention policy modelled in `t6.llm`.

The recording channel is chat ID plus topic ID (the whole chat when no topic is configured).
Both are explicitly configured, never guessed from
a chat title, Project link, membership or message content. Without that
chat configuration no chat qualifies. Only that exact chat and configured topic admit text for
capture or a photo for the label read. Both capture admission and photo
resolution must enforce this boundary.

Every other chat remains under ADR 0007, including its existing brief-reply
and command rules. Linking a chat to a Project grants no recording rights.
This amendment authorizes drive recording only, not retaining unrelated
chatter. QR labels and QR scanning are not used for recording.

## Consequences

- The recording-input guard is exercised in `tests/test_drive_recording.py`:
  only an exact configured chat ID and topic ID admit text and photo, and missing chat
  configuration admits neither. Capture persistence and LLM resolution are
  built by the subsequent tickets in #615 and must use this boundary.
- The existing ADR 0007 guard tests in `tests/test_linked_chats.py` and
  `tests/test_concern.py` remain in force. Linked-chat fact ingestion is
  unchanged, including for TRANSMITTAL: recording goes through the capture verb.

## Amendment: Jev first, text-only posts and the debug topic (#659)

Date: 2026-11-24. Source: [spec #659](https://github.com/mclbautista/t6-core/issues/659).

- On a recording-channel post that has passed the gateway's local check, Jev
  (the transmittal check) runs **before** the photo read. Its input is the
  withheld caption and a has-photo flag, never photo readings. Only an
  explicit "no" discards the post (state ignored, text and photo deleted, the
  image model never called); a failure or "maybe" keeps it. Follow-up answers,
  custodian-picked Drives and caption edits skip Jev. With no Jev provider
  configured the post is kept.
- A **text-only** post (no photo) in the configured chat and topic is a
  Capture. If Jev keeps it, its Items wait in the Inbox for the custodian
  (#724). It is
  never sent to the image model. Recording a movement from a text-only post
  without the custodian is out of scope.
- The gateway's **debug topic** copies withheld captions to a Telegram topic.
  They are not stored in t6.
- Prompt answers and undo replies are verb input from the poster or a
  custodian, like any other recording-channel input.


## Amendment: photo-to-photo Drive matching (#693, #727)

Date: 2026-11-25. Source: [spec #693](https://github.com/mclbautista/t6-core/issues/693).

The photo of a recording-channel post and the stored **Reference photos of
shortlisted Drives** may go to the image model to **match** Drives, as well as
read visible labels. `drive-photo-match` replaces `drive-photo-read` in Capture
resolution. Each Item sends its caption and a direction-filtered shortlist of
at most six Drives, the post photo shrunk to about 1600 px on its long side,
and available Reference photos as small thumbnails labelled A–F. A Drive without
a Reference photo is listed as text only. No surrounding conversation or other
chat content is authorized.

Jev still runs first and unchanged. An empty shortlist or text-only post never
reaches the image model. Admission and resolution both enforce the exact
configured recording chat and topic; changing or removing that configuration
cannot authorize a stored photo. Missing vision providers, errors, timeouts and
invalid output keep the Capture and leave its Item waiting in Inbox with no
guess or Movement. Valid vision guesses merge with the confirmed 0.95 exact-match
rule, which stays first. Only guess one creates a Movement, marked to review.

The HTTP boundary tests in `tests/test_drive_capture.py` and
`tests/test_drive_photo_match.py` guard this authorization, including a valid
shortlist in a stored Capture whose chat or topic no longer qualifies. Synthetic
photo-match evals run offline with the fake provider in CI; a recorded live run
with human review remains a release prerequisite under
`docs/agents/llm-task-specs.md`.
