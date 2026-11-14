# Google Calendar fact check for Bay bookings (t6-core#186)

Research date: 2026-11-14. Scope: [issue #186](https://github.com/mclbautista/t6-core/issues/186),
a fact check under [spec #178](https://github.com/mclbautista/t6-core/issues/178).

This is a review of public documentation. It is not a live test. No Google
account, calendar or credential was created or read. Provider rules can
change. Each section says what is documented and what is still unverified
for T6. This note feeds the Google adapter and sync tickets (B2, B3, B4) and
the human ticket B5 that creates the real credential and the Bay calendars.

**Facts from Cyril (2026-11-14).** T6 uses a personal Gmail account, not
Google Workspace. The Bay calendars belong to that account's calendar. The
calendars needed are Bay 1, Bay 2, Bay 3, Bay 4 and Viewing Room (five).

## Recommendation (account setup)

Use one service account as the only credential. Keep the five calendars owned
by the personal Gmail account. Share each calendar to the service account's
email address with the role "Make changes and manage sharing" (API role
`owner`, manager access). Keep the service account key only in the t6 API
env file on Sixto.

Reasons:

- A personal Gmail account cannot use domain-wide delegation. The Google
  guide ties it to a Workspace administrator
  ([service accounts](https://developers.google.com/identity/protocols/oauth2/service-account)).
- A service account key has no refresh token and no 7-day or 6-month expiry.
  A person never has to sign in again. This matches the standing rule of one
  owner for each credential.
- Google advises against creating calendars as a service account, because the
  service account becomes the data owner
  ([calendars.insert](https://developers.google.com/workspace/calendar/api/v3/reference/calendars/insert)).
  So the calendars stay owned by the Gmail account and are made by a person.

**Fallback.** Use an OAuth user credential for the Gmail account. Publish the
consent screen to Production. A project in "Testing" status gets a refresh
token that expires in 7 days, and a token unused for six months stops working
([OAuth 2.0 overview](https://developers.google.com/identity/protocols/oauth2)).
This option needs a person to sign in again after any revocation.

**Unverified (test in B5 before B2 builds on it).** The pages read do not say
that a service account can use a calendar shared from a personal account, or
that the `owner` role lets it change sharing. Test on one calendar: share it
to the service account, call `calendars.get`, create and delete one event, and
insert and delete one reader rule. If the test fails, use the fallback.

## 1. Which Google account, and who can own the calendars

**Documented.**

- A service account "belongs to your application instead of to an individual
  end user". Acting for a user needs domain-wide delegation, and the steps
  need a Workspace super administrator
  ([service accounts](https://developers.google.com/identity/protocols/oauth2/service-account)).
  T6 has no Workspace, so that route is closed.
- `calendars.insert` makes the authenticated user the data owner. "Don't use a
  service account for authentication" for it, because the service account
  would own the calendar
  ([calendars.insert](https://developers.google.com/workspace/calendar/api/v3/reference/calendars/insert)).
- `calendarList.insert` adds an existing calendar by `id`
  ([calendarList.insert](https://developers.google.com/workspace/calendar/api/v3/reference/calendarList/insert)).
- The ACL `owner` role is manager access. It "is different from the calendar's
  data owner"
  ([ACL](https://developers.google.com/workspace/calendar/api/v3/reference/acl)).

**Unverified for T6.** Sharing a personal calendar to a service account, as
described in the recommendation. Also: if T6 later gets Workspace, the account
setup can be reviewed again.

## 2. Setting editor and reader sharing through the API

**Documented.** `acl.insert` creates a rule with a `role` and a `scope`.

- Roles: `none`, `freeBusyReader`, `reader`, `writerWithoutPrivateAccess`,
  `writer`, `owner`. `writer` is "read and write access".
- Scope types: `default` (public), `user`, `group`, `domain`. `scope.value` is
  an email address or a domain.
- Scopes needed: `calendar` or `calendar.acls`.
- `sendNotifications` defaults to true. Set it to false so a grant sends no
  email.
- Methods: insert, list, delete, patch. A patch costs three quota units; the
  page advises `get` then `update`.

Sources: [ACL resource](https://developers.google.com/workspace/calendar/api/v3/reference/acl),
[acl.insert](https://developers.google.com/workspace/calendar/api/v3/reference/acl/insert).

**How this maps to the `bookings` permission.** For each Person with a Google
email: `person grant bookings` inserts a `writer` rule on each Bay calendar,
`person revoke bookings` deletes it, and everyone else gets a `reader` rule.
Do not use the `default` scope; it makes the calendar public.

**Unverified for T6.**

- Each Person needs a Google account email, and the Person record has no such
  field yet. A Google Group as a `group` scope could shorten the list; this
  was not checked.
- The pages do not say who may call the ACL methods beyond the scopes above.
  The B5 test covers it.

## 3. Push notifications (`events.watch`)

**Documented.**

- The address must use HTTPS with a valid certificate. A self-signed,
  untrusted, revoked or mismatched certificate gets no notifications
  ([push notifications](https://developers.google.com/workspace/calendar/api/guides/push)).
  This is why the notification route lives on the VPS edge.
- Request: `POST /calendars/{calendarId}/events/watch` with `id` (a unique
  string), `type` `web_hook`, `address`, optional `token` and optional
  `params.ttl`
  ([events.watch](https://developers.google.com/workspace/calendar/api/v3/reference/events/watch)).
  One channel watches one calendar, so T6 needs five channels.
- `token` is any string up to 256 characters. Google returns it in the
  `X-Goog-Channel-Token` header of every notification. Check it on each call.
  Do not put a secret such as an OAuth token in it.
- Other headers: `X-Goog-Channel-ID`, `X-Goog-Message-Number`,
  `X-Goog-Resource-ID`, `X-Goog-Resource-State` (`sync` on creation, `exists`
  on a change), `X-Goog-Resource-URI`. Message numbers increase but are not
  sequential.
- A notification has no change details. A later API call reads the change.
  The push guide also says some notifications can carry a body. The page does
  not settle this; do not depend on a body.
- Expiry: the default `ttl` is 604800 seconds (7 days). The pages state no
  maximum. The channel expires at the more restrictive of the request and
  Google's internal limits.
- Renewal: "there's no automatic way to renew". Call `watch` again with a new
  unique `id` before expiry. The two channels overlap for a short time, so the
  handler must accept both and ignore duplicate work.

**Confirmed answer to "at most about a week".** Not confirmed. A week is the
documented default. The maximum is not documented. Design for a week or less.

**Recommended design for B3.** Request an explicit `ttl`, keep the returned
`expiration`, and renew each channel daily with a job. Check the
`X-Goog-Channel-ID` against known channels and the token on every call.
Run a full list from the sync token each day as a safety net, because a lost
notification is not reported.

**Unverified for T6.** The edge route and a live notification. Stopping an old
channel (the `channels.stop` method) was not read.

## 4. Sync token, and who edited an event

**Sync token (documented**,
[sync guide](https://developers.google.com/workspace/calendar/api/guides/sync)**).**

- A full `events.list` returns `nextSyncToken` on the last page only. Store it.
- After a notification, call `events.list` with `syncToken`. The answer holds
  only the changes, and a new `nextSyncToken` to store.
- The result "will always contain deleted entries" so t6 can remove them.
- Filters are restricted with a sync token. A request with a disallowed
  parameter gets HTTP 400. Use the same parameters on the first and later
  calls, and apply date filters only in t6.
- HTTP 410 means the token is invalid. Delete the local state and do a full
  sync.

**Who edited an event.** Not available from the Events resource. The resource
has `creator`, `organizer`, `updated`, `etag` and `sequence`, and no field for
the last user who changed it
([events resource](https://developers.google.com/workspace/calendar/api/v3/reference/events)).

**Consequences for the rules.**

- t6 cannot say which Person dragged an event. The permission check is the
  calendar sharing itself: only a `writer` can edit, so only a `bookings`
  holder can. A reader edit cannot happen.
- t6 must still apply its rules (overlap, Bay, Project). It compares each
  changed event with its Booking and reverts a clash with a note.
- t6 must know which changes are its own writes, which also send
  notifications. Store the `etag` that t6 last wrote for each event. A change
  with a different `etag` is an edit by a person. This is a design inference,
  not a documented feature.
- Link an event to its Booking with a private extended property such as
  `t6g=<Booking id>`. Keys can have 44 characters and values 1024. Events
  can be listed by `privateExtendedProperty`
  ([extended properties](https://developers.google.com/workspace/calendar/api/guides/extended-properties)).
  A person can delete or change a property in the Calendar interface; verify
  in B2 that a drag keeps it.

**Unverified for T6.** The `etag` echo rule and that a drag keeps the property.
Audit by user would need Workspace admin reports, which T6 does not have.

## 5. Quotas

**Documented**
([quota guide](https://developers.google.com/workspace/calendar/api/guides/quota)).

- 10,000 requests a minute for each project.
- 600 requests a minute for each user for each project.
- A sliding one-minute window.
- A daily threshold of 1,000,000 requests a project that cannot be raised.
  Use under it is not billed. Billing is planned later in 2026 with at least
  90 days' notice.
- A quota error is HTTP 403 or 429 with `usageLimits`. Use truncated
  exponential backoff: wait `min(2^n + random up to 1000 ms, 32 or 64 s)` and
  stop after a limit.

**For T6.** Five calendars, one channel each, and a few booking changes a day
use a very small part of these limits. A daily renewal and a daily list add
about 10 calls a day. The limit that matters is a loop of ACL patches (three
units each) when many Persons change at once; space those calls.

## Open items for B5 and the adapter tickets

- B5 (human): create the service account and key, create the five calendars in
  the Gmail account, share them, and run the one-calendar test in section
  "Recommendation". Keep the key only in the Sixto env file.
- B2: use a fake Google Calendar for all tests; add the `etag` and private
  property checks above.
- B3: renewal job, token check, 410 handling.
- B4: add a Google email field to Person, or confirm the Group approach.
