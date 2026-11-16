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

**New Project continues on the same card (#406).** On an `unbound_page` card, **New Project**
registers the Project (out of rotation, no code) as before. The card then does not close: it names
the new Project and asks for a code (stage `code`, `enter` set). `/project CODE` sets the code and
puts the Project in rotation in one step, and the card shows that it was registered, its code and
that it is in rotation. **Keep out of rotation, no code** closes the card with "Registered as a new
Project (out of rotation, no code)". A text that cannot be a code is asked for again. A code another
Project holds gives the choices of **A held code** below, on this card. `page_taken` and `stale`
close the card as before. The tap answers `registered` for the registration; the card keeps its
kind, and its `code` and `conflict` stages use the same logic as a `/project` card.

## The card

`operator.deliver` jobs (main queue) read `GET /operator/actions/{token}/card`
(`operator` grant) and call the gateway: `/notify` with
`idempotency_key: op-card:{token}`, later `/edit` with
`op-edit:{token}:{version}`. Once `GET /notify/{ref}` says `sent` the worker calls
`POST /operator/actions/{token}/delivered` with `message_ref` and `message_id`.
A tap that arrives first, from the card's chat and with an integer `message_id`, records that `message_id` and is carried out at once; the confirmation then agrees with it, and a first delivery of a card past version 1 also edits it to the current text (key `op-edit:{token}:{version}`). A tap without a usable `message_id` before confirmation is answered 503 `card_not_confirmed` and the gateway retries.

Text is HTML with every Notion value and Project Code escaped. A callback payload is
`op:<token>:<choice>`: at most 53 bytes, so the gateway's `mod:t6core:` prefix
stays within Telegram's 64.

## The tap

The gateway pushes `{"data": "op:<token>:<choice>", "from": {"id"}, "chat",
"message_id", ...}` to the API's `POST /tap` with `Idempotency-Key: tap-<update_id>`,
`Authorization: Bearer <gateway secret>` and `X-Telegram-User-Id` matching
`from.id`. Bind that secret to `gateway` in `T6_API_SECRETS`. Only an active
Person can tap; a missing Person is refused and recorded.

| Answer | Meaning |
| --- | --- |
| 200 `{"outcome": ...}` | decided: `registered`, `bound`, `classified`, `choose_project`, `skipped`, `no_candidates`, `page_taken`, `code_held`, `frameio_list`, `frameio_search`, `frameio_linked`, `frameio_held`, `frameio_refused`, `stale`, `expired`, `already_handled`, `busy` or `rejected`. The gateway stops retrying |
| 503 | not decided (card not confirmed yet, registry unavailable, operator actions off). The gateway retries |

Each action stores an allowed-tapper rule. The default is `permission:operator`;
other rules are `person:<t6n_ID>`, `permission:<name>`, `role:staff` (any active staff
Person), or
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
| `/project TEXT` with no card waiting for text | for an operator only. TEXT is cut to 40 characters. If TEXT is, as a code, the code of exactly one Project, opens that Project's card (as if picked from the list). Otherwise opens a list of the Projects, in rotation or not, whose code contains TEXT or whose T6 ID ends with TEXT (case-insensitive), five to a page, with **Next** and **Previous** that keep the search. With no match the card says so and offers both lists. Nothing is changed |

A card waiting for a code or a Frame.io search takes the typed text first. The staff card is the
same for any text. The Project is picked from a list core keeps with the card, by T6 ID and
revision. The button label is only a display.

**Link Frame.io project (#272).** With the API able to read the Frame.io worker
(`T6_FRAMEIO_READ_URL`, `T6_FRAMEIO_READ_SECRET`), a Project's card has this button. It
lists the Frame.io projects of the configured T6 account that no show holds (every project
with an active frameio-project Media location is hidden), most recently updated first, five
to a page. **Search** waits for `/project TEXT` and narrows the list by name; **Clear search**
brings it back. Tapping a project runs `frameio project link` for the card's Project as the
tapper. If another show holds it by then, nothing changes and the card names the holder
(`frameio_held`). Any active staff Person may use it: a staff Person who is not an operator
sends `/project` in their own chat and gets the card with only this button (rule `role:staff`);
the code and rotation buttons are refused on such a card.

**A Frame.io project with no show.** When review activity arrives for a Frame.io project no
show holds, the main worker holds it and asks (`POST /operator/offers`,
`{"kind": "frameio_link", "frameio_project_id": ...}`) for one card per Frame.io project, with
rule `permission:coordinator`. The card lists shows (in rotation first, five to a page);
tapping one runs `frameio project link`, and the held activity is then delivered once. **Not
now** leaves it held.

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
   Set `t6core.delivery: push` and `t6core.push_url` in the gateway configuration
   to the API's LAN address, for example `http://<t6-api-lan-address>:8081/tap`.
   The API accepts a push only when its bearer is bound to the identity `gateway`
   in `T6_API_SECRETS` and its `X-Telegram-User-Id` header equals `from.id` in the
   body. The gateway sends its t6core module secret as the bearer, so add that
   secret to `T6_API_SECRETS` mapped to `gateway`; the header needs a gateway
   that sends it (t6-sixto-gateway #105 or later). `T6_TAP_SECRET` plays no part
   on the API listener. The API listener must be reachable on that LAN address.
   The gateway no longer needs a Tailscale sidecar for tap delivery. The VPS
   `/tap` is unavailable in edge mode (#184).
3. Send `/project` as an operator: the list arrives. Send it from another
   account: the gateway refuses it. Tap a button: the card changes a few seconds
   later, when the main worker has run the delivery job.

To roll back, remove the `project` command from the gateway config and restart
it. Cards already sent stay answerable while core runs this version.

## Approvals

An Approval is a t6 database record, independent of Frame.io. `approval request`
accepts the gates `vfx-committee`, `client-cut`, `grade`, `delivery-sign-off`,
`drive-wipe`, `fleet-promotion`, and `project-start` (the Approval `project start` creates); the corresponding `request-<gate>` aliases
supply the gate. The target can be a T6 ID or a free reference. The optional
`--tapper-rule` accepts the same rules as operator actions (JSON for `any_of`)
and defaults to `permission:operator`. A single `person:<id>` rule sends the
card to that active Person's Telegram ID when available; other rules use the
configured operator chat.

A request is previewed and confirmed before the card is created. The adapter
uses one operator action per Approval and restores missing cards from requested
Approvals when the server starts. The normal gateway delivery and single-use
tap checks apply. Grant is one tap. Denial asks the tapper to choose a reason
from the card; `approval deny` accepts a free-text `--reason` instead. CLI
and card decisions apply the same allowed-tapper rule. A decision through the
CLI also closes the card. The database keeps the decision and its audit and
Activity entries even if card delivery fails.
