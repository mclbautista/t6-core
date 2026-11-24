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
  Capture. If Jev keeps it, it is an exception with reason `no_photo`. It is
  never sent to the image model. Recording a movement from a text-only post
  without the custodian is out of scope.
- The gateway's **debug topic** copies withheld captions to a Telegram topic.
  They are not stored in t6.
- Prompt answers and undo replies are verb input from the poster or a
  custodian, like any other recording-channel input.

