# Operator actions

How t6-core and the Sixto gateway divide the work when an operator changes the
Project registry from Telegram. The gateway stays a transport: it never reads the
registry or decides who may act. Everything below uses routes the gateway
already has (`/notify`, `/edit`, `GET /notify/{ref}`, push delivery of taps).

## Who does what

| Step | Done by |
| --- | --- |
| Notice a page no Project owns, or an unclassified Project | core (`notion.page_changed` reconcile, `POST /operator/offers`) |
| Keep the pending action, its token and expiry | core (`T6_OPERATOR_DB`) |
| Send the card, edit it later | core's main worker, through the gateway |
| Carry the tap to core | gateway (push delivery to `POST /tap`) |
| Check the tapper, the chat, the message, the revision and single use; change the registry | core |
| Show the result and the Notion sync outcome | core's main worker, by editing the card |

## Offering a card

`POST /operator/offers` on the tailnet listener. Grants: `operator` (the main
worker) or `registry_write`.

```json
{"kind": "unbound_page", "page_id": "<Notion page>", "label": "<page title>"}
{"kind": "classify", "project_id": "t6p_..."}
```

Answer `200 {"offered": true, "created": true, "token": "..."}`. Offering the same
page or the same Project revision again returns the live card instead of a new one.
A `classify` offer for a Project that is not unclassified answers `{"offered": false}`.

## The card

`operator.deliver` jobs (main queue) read `GET /operator/actions/{token}/card`
(`operator` grant) and call the gateway: `/notify` with
`idempotency_key: op-card:{token}`, later `/edit` with
`op-edit:{token}:{version}`. Once `GET /notify/{ref}` says `sent` the worker calls
`POST /operator/actions/{token}/delivered` with `message_ref` and `message_id`.
Until then core retries taps on the card (503) rather than deciding.

Text is HTML with every Notion value escaped. A callback payload is
`op:<token>:<choice>`: at most 53 bytes, so the gateway's `mod:t6core:` prefix
stays within Telegram's 64.

## The tap

The gateway pushes `{"data": "op:<token>:<choice>", "from": {"id"}, "chat",
"message_id", ...}` to `POST /tap` with `Idempotency-Key: tap-<update_id>`.

| Answer | Meaning |
| --- | --- |
| 200 `{"outcome": ...}` | decided: `registered`, `bound`, `classified`, `choose_project`, `skipped`, `no_candidates`, `page_taken`, `code_held`, `stale`, `expired`, `already_handled`, `busy` or `rejected`. The gateway stops retrying |
| 503 | not decided (card not confirmed yet, registry unavailable, operator actions off). The gateway retries |

`rejected` covers a user not on `T6_OPERATOR_IDS`, a wrong chat or message, an
unknown token and an unknown choice; each is recorded and changes nothing.
Other `/tap` payloads still queue the generic `tap` job.
