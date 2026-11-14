# Operator actions

How t6-core and the Sixto gateway divide the work when an operator changes the
Project registry from Telegram. The gateway stays a transport: it never reads the
registry or decides who may act. Everything below uses routes the gateway
already has (`/notify`, `/edit`, `GET /notify/{ref}`, push delivery of taps),
plus push delivery of the `/project` command.

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

Text is HTML with every Notion value and Project Code escaped. A callback payload is
`op:<token>:<choice>`: at most 53 bytes, so the gateway's `mod:t6core:` prefix
stays within Telegram's 64.

## The tap

The gateway pushes `{"data": "op:<token>:<choice>", "from": {"id"}, "chat",
"message_id", ...}` to `POST /tap` with `Idempotency-Key: tap-<update_id>`.

| Answer | Meaning |
| --- | --- |
| 200 `{"outcome": ...}` | decided: `registered`, `bound`, `classified`, `choose_project`, `skipped`, `no_candidates`, `page_taken`, `code_held`, `stale`, `expired`, `already_handled`, `busy` or `rejected`. The gateway stops retrying |
| 503 | not decided (card not confirmed yet, registry unavailable, operator actions off). The gateway retries |

Each action stores an allowed-tapper rule. The default is `permission:operator`;
other rules are `person:<t6n_ID>`, `permission:<name>`, or
`{"any_of": [<rule>, ...]}`. At tap time core resolves the Telegram ID to an
active Person and checks their current permissions. A refused tap records the
rule in `rejections`. A wrong chat or message, unknown token, or unknown choice
is also rejected and recorded; none changes the action. `T6_OPERATOR_IDS` still
selects the destination for existing operator cards and authorizes `/project`.
Other `/tap` payloads still queue the generic `tap` job.

## Project Codes and rotation (`/project`)

The operator starts these, so the gateway also pushes one command to `POST /tap`
(gateway `commands.project` owned by the `t6core` module, mclbautista/t6-sixto-gateway#72):

```json
{"kind": "command", "command": "project", "args": "", "from": {"id": 42},
 "chat": 42, "message_id": 77, "update_id": 9001}
```

with `Idempotency-Key: cmd-<update_id>`, the same on every retry. Core checks the
sender against `T6_OPERATOR_IDS` itself and takes the command only from the
operator's own chat with Sixto; the gateway's allow-list is not the authority.
Any other pushed command answers `200 {"outcome": "ignored"}`.

| Sent | What core does |
| --- | --- |
| `/project` | opens a card listing Projects in rotation, five to a page, each with its code and T6 ID. **Historical Projects** lists the ones out of rotation or unclassified |
| a Project's button | shows its rotation and Notion sync state with **Put in rotation** or **Take out of rotation**, **Change code**, **Clear code**, **Close** |
| **Change code**, then `/project NEWCODE` | the card waits for the code; the newest waiting card of that operator takes it. A code with no letter or digit, or over 40 characters, is asked for again |
| `/project NEWCODE` with no card waiting | changes nothing and opens the list |

The Project is picked from a list core keeps with the card, by T6 ID and
revision. The button label is only a display.

**A held code.** When a Project cannot enter rotation because its code is held,
nothing changes and the card asks how to resolve it:

- **Clear this Project's code and enter**: clears it, then enters with no code.
- **Change this Project's code**: waits for `/project NEWCODE`, sets it, then
  enters. If that code is held too, the card asks again.
- **Take <holder> out of rotation**: offered when one Project holds the code.
  The holder leaves rotation (or is classified out) and keeps its code on record;
  then this Project enters.
- **Cancel**: nothing changes.

Each step is its own registry change with the operator as actor and its own
idempotency key (card token, card version, choice), so a redelivered tap or
command repeats nothing. The holder is taken out first; if the Project then
still cannot enter, the card says so and asks again.

Outcomes added to the `/tap` answer: `opened`, `project`, `enter_code`,
`bad_code`, `conflict`, `rotation_in`, `rotation_out`, `code_set`,
`code_cleared`. `code_held` also answers a code typed for a Project in rotation
that another Project in rotation holds. A used card shows what the registry
accepted and, on its own line, where Notion stands: pending, updated, or failed
after retries. A resolution that took a holder out shows a Notion line for each
of the two Projects. A Notion failure never changes the first line.

### Rollout

Core first, then the gateway. An older gateway never pushes commands, so the
new core is safe on its own; a newer gateway against an older core would have
its `/project` pushes queued as empty `tap` jobs.

1. Deploy core with this change (server and main worker). `T6_OPERATOR_IDS`,
   `T6_TAP_SECRET`, `T6_GATEWAY_URL` and `T6_GATEWAY_SECRET` stay as they are.
2. Deploy the gateway with pushed commands (#72), add the `project` command to
   its `config.yaml` with the same people as `T6_OPERATOR_IDS`, and restart it.
   The push uses the tailnet-only tap URL the taps already use (#57).
3. Send `/project` as an operator: the list arrives. Send it from another
   account: the gateway refuses it.

To roll back, remove the `project` command from the gateway config and restart
it. Cards already sent stay answerable while core runs this version.
