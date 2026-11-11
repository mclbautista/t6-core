# Integration fact check (t6-core#13)

Research date: 2026-11-09, with an update on 2026-11-11. Scope:
[issue #13](https://github.com/mclbautista/t6-core/issues/13) and
[spec #1](https://github.com/mclbautista/t6-core/issues/1).

This is a review of public documentation. It is not a live test of T6
accounts. Provider rules can change. Each section says what is documented and
what is still unverified for T6. The full first write-up is the
[issue comment of 2026-11-09](https://github.com/mclbautista/t6-core/issues/13#issuecomment-6743279874).

## Frame.io V4 authentication and refresh

**Documented.** Frame.io V4 uses Adobe IMS OAuth. An IMS refresh token expires
after 14 days by default. For an OAuth Web App, request and get consent for
`offline_access`. A refresh returns a new access token and a new refresh
token. If each new refresh token is stored and used, access tokens can be made
with no end date. A daily refresh fits this model. It is not a guarantee:
revocation, missed refreshes, service failures and a lost rotated token can
each need a human sign-in.

- [Adobe IMS token reference](https://developer.adobe.com/developer-console/docs/guides/authentication/UserAuthentication/ims#fetching-access-tokens)
- [Adobe refresh flow and revocation](https://developer.adobe.com/developer-console/docs/guides/authentication/UserAuthentication/#fetching-new-access-tokens-using-a-refresh-token)
- [Frame.io V4 authentication](https://developer.frame.io/platform/docs/guides/authentication/overview)

**Access token lifetime.** Adobe gives a default of 24 hours. The Frame.io
overview gives about one hour. Use the `expires_in` of the token response, not
a fixed lifetime. Do not apply the legacy Frame.io `offline` scope and its
30-day refresh token to V4
([legacy token guide](https://developer.frame.io/platform/v2/refreshing-oauth-2-tokens)).

**Credential types.** Adobe says Single Page App and Native App credentials
get no refresh token. Use the Web App flow. Frame.io also describes
server-to-server service accounts for accounts administered through the Adobe
Admin Console; whether the T6 account can use them is unverified.

- [Adobe credential types](https://developer.adobe.com/developer-console/docs/guides/authentication/UserAuthentication/implementation#user-authentication-credential-types)

**Update 2026-11-11 (read while building t6-core#15).**

- Authorize: `GET https://ims-na1.adobelogin.com/ims/authorize/v2` with
  `client_id`, `redirect_uri`, `scope`, `response_type=code` and `state`
  (up to 4096 characters, returned unchanged).
- Token: `POST https://ims-na1.adobelogin.com/ims/token/v3`. A Web App
  (confidential client) sends `Authorization: Basic base64(client_id:client_secret)`.
  Grants: `authorization_code` with `code`, and `refresh_token` with
  `refresh_token`.
- Answer fields: `access_token`, `refresh_token` (only with `offline_access`),
  `token_type` (`bearer`), `expires_in` (seconds).
- Frame.io Web App scopes: `openid email profile offline_access additional_info.roles`.
- The API call carries `Authorization: Bearer <access token>`.
- Adobe's reference does not give the error body or status of a refused token
  call. `t6.frameio.tokens` treats 400, 401 and 403 as "a person must sign in"
  (the OAuth standard). This is unverified against Adobe.

**Unverified for T6.** No T6 credential has been tested.

## Adobe OAuth redirect URIs

**Documented.** An Adobe OAuth Web App redirect must be HTTPS, localhost
included. Adobe gives `https://localhost/redirect/` as an example. Plain HTTP
localhost is not supported for a Web App, although a Frame.io SDK example uses
it. A Telegram link opened on a phone resolves `localhost` on the phone, not
on the Mac, so an HTTPS localhost listener only works when the browser that
signs in can reach it.

The default URI must be absolute, HTTPS, at most 256 characters, with no
wildcard. A requested alternative must match the configured pattern (512
characters at most). The IMS reference names the parameter `redirect_uri`; the
implementation guide says `redirect_url`. A missing or non-matching requested
URI falls back to the default. Development credentials allow redirect URI
changes. A production change needs an Adobe support ticket.

- [Adobe implementation guide](https://developer.adobe.com/developer-console/docs/guides/authentication/UserAuthentication/implementation#understanding-default-redirect-uri-and-redirect-uri-patterns)
- [IMS authorize reference](https://developer.adobe.com/developer-console/docs/guides/authentication/UserAuthentication/ims)
- [Development and production](https://developer.adobe.com/developer-console/docs/guides/authentication/UserAuthentication/)

**Unverified for T6.** The exact configuration and callback on the real
credential.

## Frame.io V4 webhooks

**Events.** Subscriptions belong to a workspace and need a public HTTPS
endpoint. Published events include `comment.created` (replies included),
`comment.updated`, `comment.deleted`, `comment.completed`,
`comment.uncompleted` and `file.versioned`. `file.created` can come before the
upload is complete; `file.upload.completed` and `file.ready` mark upload and
transcode completion. There is no approval event in the published list.
`metadata.value.updated` exists, but the documentation does not say that an
approval action sends it. A payload names the resource; details need an API
read.

- [Frame.io V4 webhook guide](https://developer.frame.io/platform/docs/guides/webhooks)
- [Webhook creation reference](https://developer.frame.io/platform/api-reference/webhooks/create)

**Signing.** The secret of a webhook is shown only in the create response.
Check `X-Frameio-Request-Timestamp` and `X-Frameio-Signature`: HMAC-SHA256 over
`v0:{timestamp}:{body}`, written as `v0=` plus the hex digest. Keep the exact
body received and compare in constant time. Frame.io recommends a five-minute
window. Its sample checks 500 seconds and accepts future timestamps, so do not
copy that check.

- [Frame.io signing guide](https://developer.adobe.com/frameio/guides/Webhooks/)

**Limits and delivery.** Webhooks and Custom Actions share 1,000 events a
minute and 20,000 an hour for each account. Events over the limit are dropped,
not queued. The Adobe-hosted guide gives five delivery attempts with
exponential backoff from 15 seconds; the Frame.io-hosted guide leaves the retry
section blank. Where a missed event matters, reconcile by reading.

**Unverified for T6.** Which approval action T6 uses and whether it sends an
event; live signatures and delivery.

## Frame.io V4 API (update 2026-11-11, read while building t6-core#16)

Base URL `https://api.frame.io`. Lists are paged: `page_size` (1 to 100,
default 50), an opaque `after` cursor, and `links.next`, a relative path to the
next page. Most routes allow 100 calls a minute for each account user; the
answer carries `x-ratelimit-limit`, `x-ratelimit-remaining` and
`x-ratelimit-window`.

| Read | Route |
| --- | --- |
| Accounts of the signed-in user | `GET /v4/accounts` |
| Projects of an account | `GET /v4/accounts/:account_id/projects` |
| Shares of a project | `GET /v4/accounts/:account_id/projects/:project_id/shares` |
| Activity of a share | `GET /v4/accounts/:account_id/shares/:share_id/activities` |
| Comments on a file | `GET /v4/accounts/:account_id/files/:file_id/comments` |
| One file | `GET /v4/accounts/:account_id/files/:file_id` |
| Files in a version stack | `GET /v4/accounts/:account_id/version_stacks/:version_stack_id/children` |
| Metadata of a file | `GET /v4/accounts/:account_id/files/:file_id/metadata` |

- A share has `id`, `name`, `short_url`, `enabled`, `expiration` and
  `last_viewed_at`, among others.
- A share activity has `id`, `inserted_at`, `type` (the example is
  `share_viewed`), `asset_id` and `user`.
- The reference has no approval object. `t6.frameio` reads approval from the
  file's `Status` metadata field. That field name is not in the reference.
- The file reference does not say how to tell that a file is in a version
  stack. `t6.frameio` asks for the children of the file's `parent_id` and
  treats a 404 as "the parent is a folder".

Sources: the [Frame.io V4 API reference](https://developer.frame.io/platform/api-reference/accounts/index)
pages for accounts, projects, shares, comments, files, version stacks and
metadata.

**Unverified for T6.** Every route above against the T6 account; the `Status`
field name; the 404 for a folder parent; which activity types exist.

## Notion webhooks

**Projects Status.** A Project is a database page and `Status` is a page
property, so a Status change should send `page.properties_updated`, when the
connection has access and the subscription includes that event. The event
names the changed property IDs in `data.updated_properties`, not the new
value: read the page for the current Status. `data_source.schema_updated` is
for schema changes.

**Limitation.** `page.properties_updated` is aggregated. Notion can combine
fast changes, events can arrive out of order, and delivery is normally inside
one minute with a stated target of five. So neither the events nor a later
read can show every intermediate step into Project Start.

**Signing and delivery.** Notion needs a public HTTPS endpoint. At setup it
sends a one-time `verification_token`, which is the key for later signatures.
`X-Notion-Signature` is `sha256=` plus the hex HMAC-SHA256 of the exact raw
body. The URL cannot change after verification without a new subscription.
Notion retries an unacknowledged event up to eight times, the last about 24
hours after the trigger. Answer 200 after durable receipt and make handling
idempotent.

**Limits.** Follow-up reads use the API budget: per connection, 600 requests
in 60 seconds on Business or Enterprise, 180 on other plans. Handle 429 and
529 and respect `Retry-After`.

- [Notion webhooks: setup and signature](https://developers.notion.com/reference/webhooks)
- [Notion events and delivery](https://developers.notion.com/reference/webhooks-events-delivery)
- [Notion request limits](https://developers.notion.com/reference/request-limits)

**Unverified for T6.** A live Status change on a test Project.

## Instagantt Standalone

**Export.** The documented flow is in the UI, for one project: Export & Share,
Spreadsheet, Gantt chart, choose columns, Generate, Download. It gives an Excel
spreadsheet. Standalone Table View cannot be exported. Native CSV, headers,
project IDs and milestones on the T6 account are unverified.

A sanitized Standalone CSV sample has `task_id`, `task_name`, `start_date` and
`due_date`, and no project name or ID column. `t6.instagantt.read_export`
fails on it with `the export has no column for: project`. Its one task has
blank dates, so it shows neither date formats nor milestones. Do not guess the
project: decide how an export is tied to its External Links row.

**Automation.** CSV import (Standalone only) makes new tasks; it does not
update tasks. The official MCP tools make and delete separate shareable
charts with public URLs; they do not read or update existing Standalone
projects. No general account API, webhook or scheduled export was found in the
sources read. That is not proof that none exists.

- [Spreadsheet export guide](https://www.instagantt.com/help-guide/export-gantt-chart-to-excel)
- [Table View help](https://www.instagantt.com/help-guide/table-view)
- [CSV import guide](https://www.instagantt.com/help-guide/instagantt-import-csv)
- [Instagantt MCP reference](https://www.instagantt.com/agents)

**Unverified for T6.** How an export names its project; a sample with dates
and a milestone; vendor answers on API, webhooks and browser automation.

## Camera data rates and Camera Formats

**Evidence.** One rate for each Camera is not enough. Sony's ZV-E1
specifications list several rates at the same raster, chroma, bit depth and
frame rate. Blackmagic RAW has constant-bitrate ratios and constant-quality
modes whose size changes with the picture, so a fixed value is not a guarantee
for variable-rate recording.

- [Sony ZV-E1 specifications](https://www.sony.com/electronics/support/e-mount-body-zv-e-series/zv-e1/specifications)
- [Blackmagic Pocket Cinema Camera manual](https://documents.blackmagicdesign.com/UserManuals/BlackmagicPocketCinemaCameraManual.pdf)

**The planner now.** The planner workbook has a mode catalogue (camera model,
resolution, codec, bitrate in Mbps) and looks up a rate by camera, resolution
and codec. Three fields cannot tell apart two modes that differ only in frame
rate or compression. The workbook was read from an export, not the live Sheet.

**Calculation.** The Sheet computes `bitrate x cameras x days x hours a day /
1000` and labels the result TB. A plain decimal conversion is
`Mbps x cameras x days x hours a day x 0.00045`, so the Sheet gives about 2.22
times that. The owner confirmed the inputs are Mbps and the extra is
deliberate headroom. Do not remove it as an error. t6-core#24 needs the same
numbers as the Sheet.

**Recommendation, not a decision.** Keep a separate Camera Formats collection
linked to Cameras. It does not have to be a Notion database: a versioned
catalogue file can hold the same one-to-many data. Choose the store after
ownership is clear. Publish only an explicit allowlist of public fields.

## Account-specific work still needed

1. **Adobe.** Confirm the credential is an OAuth Web App with
   `offline_access`, and whether it is development or production. Test sign-in
   and token rotation on a safe listener.
2. **Frame.io.** Find which approval action is used and whether it sends an
   event. Check live signatures and delivery. Check the reads in
   "Frame.io V4 API" on the T6 account.
3. **Notion.** Change the Status of a test Project on a test subscription.
   Decide whether a missed short step into Project Start is acceptable.
4. **Instagantt.** Say how an exported file names its project. Give a sample
   with dates and a milestone. Ask the vendor about API, webhooks and
   automation.
5. **Planner.** Give reference inputs and outputs. Decide decimal or binary
   capacity, the headroom rule, the modes needed and who keeps the catalogue.
