Source: grill with Cyril 2026-11-24/25, decisions in the project files `transmittal-photo-process-grill/grill-decisions.md` (32 decisions). Builds on #659 (Jev first, edits, undo window, debug topic) and #615 (recording channel). Amends ADR 0009.

## Problem Statement

Staff record every Drive handover by posting a photo and a caption in the TRANSMITTAL Telegram group. The way they actually post doesn't match what t6 expects:

- The label is handwritten on masking tape on the **box**, rarely on the Drive. Photos show several Drives, unrelated Drives in the background, and blurry or night shots. Serials are never visible.
- Captions use the staff's own habit: `IN:` or `OUT:`, then one or more items on the same line or the following lines, `R: <name>` for the recipient, @tags, short names ("MC7 D21" for 2nd Miracle Cell #7 Day 21, "RM D07", "LSB"), and count lines ("50 Portable drives", "48 FD").
- Today t6 reads one label from the photo and needs an exact label or serial match. When it fails, Sixto asks in the group for the label. Most real posts fail this way, which makes Sixto noisy in the group and puts the work on the poster.

The custodian (Ate Angel) reviews every movement anyway. She needs good guesses she can approve in one tap. The posters need to know whether their caption was complete, without chatter. Coordinators need the movement history of their Projects.

## Solution

Keep the posting habit. t6 reads the caption with plain code, builds a small shortlist of likely Drives, and makes **one cheap vision call per named item**. The call compares the new photo with the stored photos of the shortlisted Drives and returns up to 3 **guesses**. Every item goes to the custodian for **approval**, as a DM with the guesses as buttons and on a new **Inbox** page in the Drives app. The Drive's status changes at once using guess 1, marked "to review". Her picks teach t6 short names and give each Drive a fresh reference photo, so guesses get better over time. An exact match scores 0.95 only after enough confirmed history. A separate, default-off auto-approve switch later lets 0.95 guesses skip the DM, but they still wait in the Inbox for approval.

In the group Sixto stays quiet. It reacts 👍 when the caption is complete and 🤔 when it is thin. It replies in text only when the poster must fix something: no IN or OUT, or an OUT without `R:`. Replying "undo" within an hour removes the reaction, and Sixto reacts 🙈 on the undo message.

Each movement records the Project's T6 Post Coordinator at the time of the move, separately from the recipient. Coordinators see movement histories on the Project page, through `/project`, and as counts in the Wednesday brief.

Everything fails open. If the shortlist, the vision call or the provider fails, the movement is still logged with no guess and goes to the custodian.

## User Stories

### Posters (staff in TRANSMITTAL)
1. As a poster, I want to keep posting `IN:` / `OUT:` with items on the same or following lines, so that I don't have to learn a new format.
2. As a poster, I want `R: <name>` read as the recipient of an OUT, so that the handover names who took the Drives.
3. As a poster, I want several items in one post read as several movements, so that one handover is one post.
4. As a poster, I want count lines like "48 FD" or "50 Portable drives" kept as counts, so that bulk handovers are still recorded.
5. As a poster, I want short names like "MC7 D21" understood, so that I can write the way I always have.
6. As a poster, I want a 👍 reaction when my caption was complete, so that I know I'm done without reading a reply.
7. As a poster, I want a 🤔 reaction when my caption was understood but thin, so that I learn to name items next time.
8. As a poster, I want a short text reply only when I must fix something (no IN or OUT, or an OUT without `R:`), naming the one missing thing, so that the group stays quiet.
9. As a poster, I want editing my caption to fix a missing IN, OUT or `R:` to re-read the post, so that I don't have to post again.
10. As a poster, I want to reply "undo" to my post within an hour to cancel it, so that I can fix a mistake.
11. As a poster, I want Sixto to remove its reaction and react 🙈 on my "undo" message, so that I see the cancel worked in a casual way.
12. As a poster, I want banter in the group ignored, so that Sixto never reacts to chatter.
13. As a poster, I want the time of my post used as the movement time, so that I never type dates.
14. As a poster who brings Drives IN, I want to be recorded as the receiver, so that I don't have to type my own name.

### Custodian (Ate Angel)
15. As the custodian, I want a DM for each named item with the post link, the photo and up to 3 guesses, so that I can approve with one tap.
16. As the custodian, I want each guess to show the Drive label, its Project and where it was last seen, so that I can tell the guesses apart.
17. As the custodian, I want buttons for "New drive", "Not a drive (skip)", "Type it" and "None", so that I can settle any item from the DM.
18. As the custodian, I want "None" to clear the guess and go quiet, so that hard items wait in the Inbox without nagging me.
19. As the custodian, I want an Inbox page in the Drives app listing every item waiting on approval, so that I can work through them at my desk.
20. As the custodian, I want approving in the DM or the Inbox to update both, so that I never approve twice.
21. As the custodian, I want the Drive's status to change at once using guess 1, marked "to review", so that the record is about right even before I review.
22. As the custodian, I want my picks to teach short names, so that "MC7" is never asked about again.
23. As the custodian, I want the photo of an approved item to become that Drive's newest reference photo, so that future guesses compare against how the box looks now.
24. As the custodian, I want an exact match marked ✓ and shown first, so that I can approve those quickly.
25. As the custodian, I want unfinished items listed in my one morning message, together with the T6 drives out more than 14 days, so that I get one reminder a day, not many.
26. As the custodian, I want my DMs and the morning message to go to the DMs and bots test channel during practice, so that practice traffic stays out of my private chat until Dec 3.
27. As the custodian, I want to undo a post within an hour, like the poster, so that I can cancel a wrong post.
28. As the custodian, I want a text-only movement post kept for my review, so that nothing is lost when someone forgets the photo.
29. As the custodian, I want auto-approved items, once that switch is on, to skip my DM but still wait in the Inbox for approval, so that I keep the final say.

### Coordinators
30. As a T6 Post Coordinator, I want each movement to record me as the Project's coordinator at the time of the move, separately from the recipient, so that my Projects' history is complete even after coordinators change.
31. As a coordinator, I want to see movement histories on the Drives app Project page and through `/project`, so that I can check on my Projects when I want to.
32. As a coordinator, I want the Wednesday coordinator brief to show per Project a weekly movement count ("3 OUT, 1 IN") and a line for each Drive out more than 14 days, so that I see drive traffic with no extra message.
33. As a coordinator, I want being @tagged to help t6 find my Project, so that posts tagging me land on the right Project.

### Owner (Cyril)
34. As the owner, I want each post's debug message to show the shortlist, the thumbnails sent, the guesses with reasons, the model, latency and cost, so that I can debug matching from Telegram.
35. As the owner, I want an accuracy log of guess 1, 2, 3, None or typed per approval, so that I can decide when to turn on auto-approve.
36. As the owner, I want the vision model chosen by an eval on real TRANSMITTAL photos, so that the cheapest model that works is used.
37. As the owner, I want the auto-approve switch separate from `T6_DRIVE_LIVE` and off by default, so that cutover and auto-approve are separate decisions.
38. As the owner, I want every failure to fall back to logging the movement with no guess, so that a provider outage never loses a handover.
39. As the owner, I want photos sent to the model shrunk, so that each call stays cheap and fast.
40. As the owner, I want Sixto kept out of TRANSMITTAL until this flow is live, so that the group never sees the old label prompts.

## Implementation Decisions

### Vocabulary (CONTEXT.md)
- **Capture** stays the stored post (the transmittal). New terms:
  - **Item**: one named line of a Capture's caption. It resolves to at most one Movement.
  - **Count line**: a caption line that is only a number and a kind ("48 FD"). It is stored on the Capture and never becomes a Movement.
  - **Guess**: a ranked candidate Drive for an Item, with a confidence and a one-line reason.
  - **Short name**: a learned text-to-Drive (and text-to-Project) mapping confirmed by the custodian, with a confirmation count.
  - **Reference photo**: the newest approved photo of a Drive.
  - **Approval**: the custodian's decision on an Item: a guess, a typed Drive, New drive, Not a drive, or None.
  - **Inbox**: the Items waiting on approval.
- "Exceptions" keeps sheet and data problems only. A Capture that needs the custodian is an Inbox item, not an exception.

### Flow (t6-core, drive module)
1. The gateway's local check and Jev run unchanged (#659). Jev "no" drops the post.
2. **Caption parse (plain code):** direction from a leading IN/OUT (current tolerant rule); `R:` line = recipient; @tags; the remaining lines (or the rest of the first line) split into Items and Count lines. "RM D07 (Master 02 and 03)" is one Item; splitting it is the guess's job, not the parser's.
3. **Completeness grade:** complete = direction, at least one Item or Count line, and `R:` on an OUT. Missing direction or a missing `R:` on an OUT = a scoped problem (text reply). Otherwise, if any Item had no shortlist, or the post named no Item (photo only) = thin. The grade is in the capture result for the gateway.
4. **Shortlist (plain code, no LLM):**
   - Project candidates from Short names, close spellings of Project names and codes (the existing name similarity in `t6.lookup`), and @tags. A tag of a Person who is T6 Post Coordinator of exactly one active Project picks that Project. A coordinator of several Projects only breaks a tie.
   - Drive candidates in those Projects (open Project uses), **filtered by direction**: an IN only considers Drives whose status is released; an OUT only Drives in custody. Rank up Drives last released to the poster or a tagged Person.
   - No Project found: a direction-filtered shortlist by label text over all Drives.
   - Cap at 6 Drives.
5. **Exact match:** an Item's text, or a label read from the photo, equal to a Drive's label or Short name scores **0.95** only when all hold: the custodian confirmed it for that Drive at least **3** times; no other shortlisted Drive shares it; the direction fits; the Drive has a Reference photo. Otherwise it is an ordinary ranked guess.
6. **Vision call (new LLM task `drive-photo-match`):** one call per Item with the new photo shrunk to about 1600 px on its long side, each shortlisted Drive's Reference photo as a small thumbnail labelled A to F, the caption and the shortlist as text. It returns up to 3 guesses (letter, confidence, one-line reason) and any label text read. It replaces `drive-photo-read` in the capture flow. With no shortlist it is not called.
7. **Fail open:** no provider, error, timeout or empty shortlist → the Item is logged with no guess, a Movement is not created, and the Item goes to the Inbox.
8. **Movement now, review later:** with a guess, a Movement for guess 1 is appended at once, marked **to review**. Approving a different Drive appends a correction (Movements stay append-only); None appends a correction that clears it and leaves the Item in the Inbox; Not a drive closes the Item.
9. **Coordinator on the Movement:** the Project's T6 Post Coordinator Person IDs (from the Notion mirror), stored on the Movement at the time of the move and never refreshed. Separate from the recipient.
10. **Learning on approval:** record the Short name (Item text → Drive, and → Project) with its confirmation count, set the photo as the Drive's Reference photo, and log the outcome (guess 1, 2, 3, None, typed, new, not a drive) for the accuracy log.
11. **Auto-approve switch:** deployment setting `T6_DRIVE_AUTO_APPROVE`, separate from `T6_DRIVE_LIVE`, off by default. When on, a 0.95 guess sends no DM but the Item still waits in the Inbox for approval, with the guess pre-picked.
12. **Undo:** unchanged rules (poster or custodian, one hour). Undo reverses all Movements of the Capture, including to-review ones, and closes its Inbox items.
13. **Debug topic:** the per-post debug message adds the shortlist, thumbnails sent, guesses with reasons, exact-match checks, model, latency and cost.

### Interfaces
- `drive capture` result gains: `grade` (complete, thin, problem), `problem` (missing_direction, missing_recipient or none), and `items` (each with text, guesses, state). The gateway decides reaction, text reply and DMs only from this result.
- New verbs (names indicative): `drive inbox` (list Items waiting on approval, with guesses and photo reference), `drive approve` (Item + choice: guess Drive, typed Drive, new, not a drive, none), `drive matches` (accuracy log summary). `drive approve` needs the `drives` permission.
- The custodian's DM goes through the existing gateway `/notify` with buttons; taps call `drive approve`. The DM target is configured: the DMs and bots test channel topic during practice, the custodian's private chat after Cutover. The morning reminder uses the same target and adds unfinished Items.
- Project reads expose Drive movements for a Project (for `/project` and the Drives app Project page) and weekly counts per Project (for the Wednesday brief).

### Schema
- Capture data gains Items, Count lines, grade and problem.
- New tables: Short names (text, Drive or Project, confirmations, last confirmed), Approvals / accuracy log (Item, outcome, guess rank, time, who), Reference photo pointer per Drive (reusing the movement photo folder).
- Movements gain `to_review` and `coordinators` (Person IDs at the time of the move).
- A Reload wipes Short names learned from practice data only if they point at Drive IDs that no longer exist; Short names for preserved Drive IDs stay.

### ADR 0009 amendment
- The photo of a recording-channel post, and the stored Reference photos of shortlisted Drives, may go to the image model to **match** Drives, not only to read labels. The model sees nothing else from the chat.

### Gateway (t6-sixto-gateway)
- Reactions from the capture result: 👍 complete, 🤔 thin, after t6 confirms the write. Text reply only for `missing_direction` or `missing_recipient`, naming the one missing thing. No "Recorded" reply and no UNDO button.
- "undo" reply from the poster or custodian within the window: undo in t6, remove 👍/🤔 from the post, react 🙈 on the undo message.
- Custodian DMs: one message per Item with post link, photo and buttons (up to 3 guesses, New drive, Not a drive, Type it, None). Taps limited to the custodian. A DM is edited to show the outcome after approval from either place.
- Sixto stays out of TRANSMITTAL until this release is live; re-adding it is the last rollout step.

### Drives app (t6-drives)
- New **Inbox** page, first in the navigation: Items waiting on approval, each with photo, caption, guesses and the same choices as the DM. Exceptions keeps sheet and data problems.
- Project page shows the Project's movement history, including the coordinator at the time.

### Reports (t6-notion-reports)
- Wednesday coordinator brief: per Project, the week's movement count and a line per Drive out more than 14 days.

## Testing Decisions

- Good tests check external behaviour through the seam: verb inputs and results, DMs and reactions sent, pages rendered. Never private functions or prompt text.
- **t6-core seam:** the HTTP verb API with the fake LLM provider (prior art: `tests/test_drive_capture.py`, `tests/test_drive_reminder.py`). Cases: caption habit parsing (multi-line, `R:`, @tags, count lines); grades; direction-filtered shortlist; coordinator strong tag (one Project vs several); the 0.95 rule (fewer than 3 confirmations, shared Short name, wrong direction, no Reference photo); fail open on provider error and empty shortlist; to-review Movement then approve, re-pick, None, Not a drive; learning (Short name count, Reference photo, accuracy log); auto-approve on and off; undo closes Inbox items; coordinator snapshot survives a coordinator change; the ADR 0009 boundary (no other chat or topic reaches the image model).
- **Gateway seam:** its existing drive capture tests with fake Telegram and fake t6 API: reactions per grade, scoped text replies, undo → 🙈 and reaction removed, DM buttons, taps limited to the custodian, DM edited after an Inbox approval.
- **Drives app seam:** page tests against the fake API (prior art: `tests/support.py`, the Exceptions page tests): Inbox lists, approves, and hides done Items.
- **Reports seam:** the Wednesday brief tests (prior art: `tests/test_wednesday_brief.py`).

## Eval cases

- **Task:** `drive-photo-match`: given a TRANSMITTAL photo, thumbnails of up to 6 shortlisted Drives and the caption, rank up to 3 shortlisted Drives that the photo shows for the named Item.
- **Expected properties:** guesses only name shortlisted letters; at most 3; confidence in 0 to 1; a one-line reason; label text read only when visible. Must-pass: a box whose label matches one thumbnail's box ranks that Drive first; a background Drive not named in the caption is not guess 1; an empty or unrelated photo returns no guess rather than a forced one.
- **Graders:** code checks schema, letters and counts. Guess-1 correctness is checked by code against labelled ground truth; reasons are reviewed by a person. Target: guess 1 right at least 50% of the time on real cases.
- **Synthetic cases:** committed in t6-core with generated box images and labels (prior art: `drive_photo_evals`).
- **Real cases:** the 16 TRANSMITTAL photos Cyril supplied 2026-11-25. Ground truth: Claude proposes each photo's Drive from the sheets; the custodian corrects in one pass. Kept off-repo in the project files.
- **Model choice:** the cheapest image-capable model that meets the target.
- A **live run recorded** ticket blocks the release, as for every LLM task.

## Out of Scope

- Auto-approving without the custodian (the switch only skips the DM).
- Tracking flash drives or bulk counts one by one.
- Perceptual hashing or embeddings: one vision call is the MVP.
- QR codes for recording.
- Turning `T6_DRIVE_AUTO_APPROVE` on (after a 2-week accuracy check).
- Pushing movement messages to coordinators.

## Further Notes

- This is an immediate change, not tied to the Dec 3 Cutover.
- t6 0.6.27 (#659 and gateway PR 131) ships as planned first; this work builds on its Jev-first flow, edits, undo window and debug topic. Its group label prompt, UNDO button and "Recorded" reply are replaced here.
- Real photos showed labels on boxes, short names in captions, multi-Drive photos with background Drives, and stock numbers (14, 25, HD #97) on some boxes. Stock numbers are a useful hint for the vision call, not a separate feature.
- Practice mode keeps adding "Practice" where messages are sent.
