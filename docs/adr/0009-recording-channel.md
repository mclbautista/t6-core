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

The recording channel's chat ID is explicitly configured, never guessed from
a chat title, Project link, membership or message content. Without that
configuration no chat qualifies. Only that exact chat ID admits text for
capture or a photo for the label read. Both capture admission and photo
resolution must enforce this boundary.

Every other chat remains under ADR 0007, including its existing brief-reply
and command rules. Linking a chat to a Project grants no recording rights.
This amendment authorizes drive recording only, not retaining unrelated
chatter. QR labels and QR scanning are not used for recording.

## Consequences

- The recording-input guard is exercised in `tests/test_drive_recording.py`:
  only an exact configured chat ID admits text and photo, and missing
  configuration admits neither. Capture persistence and LLM resolution are
  built by the subsequent tickets in #615 and must use this boundary.
- The existing ADR 0007 guard tests in `tests/test_linked_chats.py` and
  `tests/test_concern.py` remain in force. Linked-chat fact ingestion is
  unchanged, including for TRANSMITTAL: recording goes through the capture verb.
