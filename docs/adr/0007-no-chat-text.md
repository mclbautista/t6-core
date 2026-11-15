# 7. t6 stores no chat text, with the brief-reply exception

Date: 2026-11-15. Status: accepted. Source: spec #178, Concern grill amendments.

## Context

T6 links Telegram chats and topics to Projects (ADR 0004). Those chats hold
ordinary working conversation between the team and freelancers, including
client details. The linked-chat tickets (#243, #236) already say that t6 keeps
no message text, but the rule was written in no ADR, so a later change could
break it without anyone noticing.

## Decision

From linked Telegram chats t6 stores only:

- ids: chat, topic, message and sender;
- the `has_file` and `has_link` flags of a message;
- membership facts (who is in a chat, and when the chat or topic moved or was
  renamed).

No message text is stored, and nothing from these chats goes to an LLM.

**The one exception.** A reply addressed to Sixto on the PSR brief that names a
Concern may be kept as that Concern's append-only note, with the replier as the
actor and the message id. This covers a reply to the brief in a group and a
reply to the brief in a DM. The gateway passes a DM reply to the brief through
to t6 (t6-sixto-gateway, spec #87). A reply that names no Concern stores
nothing.

**What is not chat text.** A command a person types to Sixto, such as the note
on `/abang`, is input to a t6 verb. It is stored as that verb's own input, in
the verb's record, like the arguments of any other verb call. It is not the
chat's message text and nothing else from the message around it is kept.

## Consequences

- Two tests guard the rule: `test_subscriptions_and_private_id_only_facts` in
  `tests/test_linked_chats.py` (linked-chat facts hold ids, flags and
  membership only) and `test_brief_reply_is_the_only_chat_text_kept` in
  `tests/test_concern.py` (only a brief reply naming a Concern is kept). A
  change that stores text from a linked chat must break one of them.
- Any further exception needs an amendment to this ADR before the code that
  uses it.
- Concern notes made this way stay append-only and name their author, so a
  person can always see who wrote a stored reply.
