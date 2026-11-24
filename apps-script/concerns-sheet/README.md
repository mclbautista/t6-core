# Concerns sheet (Apps Script)

The Google Sheet is Cyril's working view of every open Concern (spec #732). `Code.gs` is its bound script. It holds no rules: t6 decides
the allowed actions of each row and the result of each Submit.

- **Refresh** (menu `Concerns > Refresh`, on open, and once about a minute after Submit) fetches the export with the read token and rewrites the data rows.
  Each row's Action dropdown comes from the export's `allowed actions` column (default Keep). An unsent Action or Note is kept by Concern id and
  dropped if that Concern is no longer in the export. The id, revision and allowed-actions columns are hidden. Every cell is plain text, so a Concern
  that starts with `=`, `+`, `-` or `@` is never evaluated.
- **Submit** (menu `Concerns > Submit`) sends only rows whose Action is not Keep or whose Note is filled, with the active user's email, signed with
  the submit secret. The sent rows show "Sent" in `last result` and go back to Keep with an empty Note. The result comes with the next refresh.
- The only timed trigger is the one-off one that Submit makes (`refreshAfterSubmit`); it deletes itself when it runs. There is no recurring trigger.
  Refresh on open is an installable on-open trigger (a simple `onOpen` may not call the network): run `Concerns > Set up refresh on open (once)` as the owner.

## Setup on the Mac mini

Run as Cyril, under his Google account, in a normal terminal. Never in a Sixto container.

1. `npm install -g @google/clasp`, then `clasp login`. Turn on the Apps Script API at https://script.google.com/home/usersettings.
2. In the sheet: `Extensions > Apps Script`. Copy the script id from Project Settings.
3. In this folder: `cp .clasp.json.example .clasp.json` and paste the script id. `.clasp.json` stays on the Mac mini and is git-ignored.
4. `clasp push` (it sends only `appsscript.json` and `Code.gs`; see `.claspignore`).
5. In the editor, Project Settings > Script Properties, add:

   | Property | Value |
   |---|---|
   | `EXPORT_URL` | the public export URL on the edge (private config) |
   | `READ_TOKEN` | the read token the edge checks in `X-T6-Read-Token` |
   | `WEBHOOK_URL` | the edge URL `/webhook/sheets/concerns` |
   | `SUBMIT_SECRET` | the sheet submit secret the verifier uses |

   These values live in private config and in Script Properties only, never in this repo. The sheet needs a tab named `Concerns` (the script makes it if missing).
6. Run `onOpen` once and accept the permissions, then `Concerns > Set up refresh on open (once)`. Share the sheet as editor with the Closers; access is Google sharing only.

## Signature check by hand

The signature is the lower-case hex HMAC-SHA256, keyed with the secret, over the bytes `{timestamp}:{body}`, sent as `X-T6-Sheet-Timestamp` (whole Unix seconds)
and `X-T6-Sheet-Signature`. The body is the exact JSON string posted. These vectors were made with `openssl` and are checked against the t6 verifier by
`tests/test_sheet_script_signature.py`. In the editor run `checkSignature` and compare the log with vector 1.

Secret `example-submit-secret`, timestamp `1764000000`:

1. Body `{"id":"c1","email":"a@b.co","rows":[]}`
   signature `213ed9dca6c55c61957347dc137cf5da135724016c09bf6351d5170465f56d6b`
2. Body `{"id":"click-1","email":"closer@example.com","rows":[{"concern":"c-1","revision":3,"action":"Close","note":"Café = ok"}]}`
   signature `3f5adb49bb93488f85503f73d2179a3bed7b24ee6b3899fbf736223a197e90ac`

## Manual check

Do this on the real sheet after the core release and the edge are live, with Cyril's yes.

1. **Refresh:** open the sheet. The rows load, the id, revision and allowed-actions columns are hidden, Action shows Keep.
2. **Close one row:** set Action to Close on one row, press Submit. The row shows "Sent". About a minute later the sheet refreshes and the row shows State "Closing after PSR" and Last result "Closing after PSR".
3. **Undo it:** on that row set Action to Undo close, Submit. After the refresh it is Open again and Last result is "Undone".
4. **Add a Note:** type a Note on a row (leave Action at Keep), Submit. After the refresh Last result is "Note added".
5. **Stale-row refusal:** change a Concern in t6 (or from a second browser) after the sheet loaded, then Submit a Close on it from the old copy. Last result is "Changed since your last refresh"; other rows in the same Submit still go through.
6. **Unsent picks survive a refresh:** set an Action and a Note on two rows, do not Submit, choose `Concerns > Refresh`. Both picks are still there, and each is still judged against the revision it was made on. Then close one of those Concerns elsewhere and refresh: its pick is gone. Last, change a Concern in t6 while a pick on it is unsent, refresh, and Submit: Last result is "Changed since your last refresh".
7. **Triggers:** `Extensions > Apps Script > Triggers` shows only the on-open trigger, and no `refreshAfterSubmit` trigger is left after the minute has passed.
8. A Concern whose text starts with `=` shows as text, not a formula.
