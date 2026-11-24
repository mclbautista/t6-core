"""File the tickets of t6-core#693 with gh, blockers first, as sub-issues of #693 with native blocked-by links.

Run once: python3 tickets-693.py. It records progress in tickets-693.state.json next to it and skips tickets already filed.
"""
import json
import pathlib
import subprocess

OWNER = 'mclbautista'
PARENT = ('t6-core', 693)
STATE = pathlib.Path(__file__).with_suffix('.state.json')
SPEC = 'mclbautista/t6-core#693'

T = []


def ticket(key, repo, title, build, criteria, blocked, label='ready-for-agent'):
    T.append(dict(key=key, repo=repo, title=title, build=build, criteria=criteria, blocked=blocked, label=label))


ticket('T1', 't6-core', 'Read the TRANSMITTAL caption habit and grade it', """\
A recording-channel caption written the way staff actually post is read into its parts, and the `drive capture` result tells the gateway how complete it was.

The caption habit: a leading `IN`/`OUT` (current tolerant rule: any case, space, colon or dash), then one or more **Items** on the same line or the following lines, an `R: <name>` line for the recipient of an OUT, @tags, and **Count lines** (a number and a kind, e.g. "48 FD", "50 Portable drives"). "RM D07 (Master 02 and 03)" stays one Item. Real examples are in the spec.

The capture result gains `items` (text, state), `counts`, `recipient`, `tags`, `grade` (`complete`, `thin`, `problem`) and `problem` (`missing_direction`, `missing_recipient` or none). Complete = direction, at least one Item or Count line, and `R:` on an OUT. Thin = understood but no Item named (photo only) or an Item that could not be shortlisted (until the shortlist exists, an Item is never thin for that reason). This replaces today's one-line parser (prefactor first). Add **Item** and **Count line** to CONTEXT.md.""", [
    'All seven real captions in the spec parse into the expected Items, Count lines, recipient and tags (synthetic names in committed tests)',
    'Missing IN/OUT gives grade `problem`, `missing_direction`; an OUT without `R:` gives `problem`, `missing_recipient`',
    'A photo-only post (direction, no Item, no Count line) is `thin`; a full caption is `complete`',
    'Editing a caption re-reads it and can change the grade (existing edit rules from #659)',
    'Tested through the HTTP verb API with the fake provider',
], [])

ticket('T2', 't6-core', 'Drive Inbox and custodian approval (no guesses yet)', """\
Every Item of a kept Capture waits in the **Inbox** for the custodian, who settles it with **Approval**. No guessing yet: this slice makes the approval path work end to end.

- New verb `drive inbox`: Items waiting on approval, newest first, with caption, Item text, direction, recipient, photo reference, guesses (empty for now) and the Capture link.
- New verb `drive approve` (needs `drives`): Item plus one choice: a Drive (typed or picked), `new` (create the Drive inline, as the existing new-drive flow), `not_a_drive` (closes the Item) or `none` (leaves it waiting, quietly). Approving appends the Movement; repeated calls are idempotent.
- Every Movement made from a Capture stores the Project's **T6 Post Coordinator** Person IDs at the time of the move (from the Notion mirror), separately from the recipient, never refreshed later.
- A Capture that needs the custodian is an Inbox item, no longer an exception. Exceptions keep sheet and data problems.
- Count lines are stored on the Capture and never enter the Inbox.
- Add **Inbox** and **Approval** to CONTEXT.md.""", [
    'A two-Item capture shows two Inbox items; approving each with a Drive appends one Movement each',
    '`none` keeps the Item waiting; `not_a_drive` closes it with no Movement; `new` creates the Drive and its Movement',
    'The Movement keeps the coordinator it had at the move after the Project coordinator changes',
    'Undo of the Capture (existing one-hour rule) reverses its Movements and closes its Inbox items',
    'No Capture becomes an exception any more; tests that expected exceptions are updated',
    'Tested through the HTTP verb API',
], ['T1'])

ticket('T3', 't6-core', 'Shortlist likely Drives and record guess 1 as "to review"', """\
For each Item, plain code (no LLM) builds a **shortlist** and ranks up to 3 **Guesses**; guess 1 becomes a Movement at once, marked **to review**.

- Project candidates: close spellings of Project names and codes (existing name similarity in `t6.lookup`) and @tags. A tag of a Person who is T6 Post Coordinator of exactly one active Project picks it; of several, it only breaks a tie (strong tag rule).
- Drive candidates: open Project uses of those Projects, **filtered by direction**: IN only Drives currently released, OUT only Drives in custody. Rank up Drives last released to the poster or a tagged Person. With no Project, fall back to a direction-filtered label-text shortlist over all Drives. Cap 6.
- Guesses carry a confidence and a one-line reason. Guess 1 appends a Movement marked to review; `drive approve` with another Drive appends a correction (Movements stay append-only); `none` appends a correction that clears it and keeps the Item waiting.
- `drive inbox` shows the guesses. An Item with no shortlist grades the Capture `thin` (T1).
- Add **Guess** to CONTEXT.md.""", [
    'IN never guesses a Drive in custody; OUT never guesses a released Drive',
    'Strong tag: coordinator of one active Project picks it; of two, only breaks a tie',
    'Guess 1 changes the Drive status at once and is listed as to review; approving it clears to review with no new Movement',
    'Re-pick and None append corrections; history shows both',
    'Empty shortlist: no Movement, Item waits, grade thin',
    'Tested through the HTTP verb API',
], ['T2'])

ticket('T4', 't6-core', 'Learn from approvals: short names, reference photos, accuracy log, 0.95 exact match', """\
Each approval teaches t6, and an exact match can score 0.95 once it has enough confirmed history.

- On approval with a Drive: save the **Short name** (Item text to Drive, and to that Drive's Project) with a confirmation count; make the Capture photo the Drive's **Reference photo** (newest approved photo, stored with the existing movement photo storage); log the outcome (guess 1, 2, 3, none, typed, new, not a drive) with who and when.
- Short names feed the shortlist (T3) as Project and Drive candidates.
- **Exact match**: an Item's text (or label text read later by T5) equal to a Drive's label or Short name scores **0.95** only when all hold: confirmed by the custodian at least 3 times for that Drive; no other shortlisted Drive shares it; direction fits; the Drive has a Reference photo. Otherwise it stays an ordinary ranked guess. Exact guesses are flagged for a check mark.
- New verb `drive matches`: accuracy summary (counts per outcome, guess-1 rate) for a date range.
- Reload: Short names and Reference photos of preserved Drive IDs stay; others are dropped.
- Add **Short name** and **Reference photo** to CONTEXT.md.""", [
    'Third confirmation of "MC7 D21" for one Drive makes it score 0.95 next time; two confirmations do not',
    'Shared Short name, wrong direction or no Reference photo each keep it below 0.95',
    'Approval sets the Reference photo and writes the accuracy log; `drive matches` reports it',
    'A Reload keeps Short names of preserved Drive IDs',
    'Tested through the HTTP verb API',
], ['T3'])

ticket('T5', 't6-core', 'Vision guesses: drive-photo-match LLM task (photo to photo)', """\
Replace the label read with one cheap vision call per Item that compares the new photo with the shortlisted Drives' Reference photos.

- New LLM task `drive-photo-match`: input = new photo shrunk to about 1600 px on its long side, up to 6 Reference photos as small thumbnails labelled A to F, caption and shortlist as text. Output = up to 3 guesses (letter, confidence 0 to 1, one-line reason) and label text read when visible. Not called with an empty shortlist.
- Its guesses merge with T4's exact-match rule (an exact 0.95 stays first). Label text it reads can trigger the exact-match check.
- **Fail open**: no provider, error, timeout or invalid output → the plain-code guesses from T3 stand (or none); never lose the Capture.
- It replaces `drive-photo-read` in the capture flow. Jev stays first and unchanged.
- Debug topic message gains shortlist, thumbnails sent, guesses with reasons, exact-match checks, model, latency and cost.
- **ADR 0009 amendment**: the post's photo and stored Reference photos of shortlisted Drives may go to the image model to match Drives; nothing else from the chat. Recording-channel boundary tests extend to the new task.
- Synthetic eval cases committed (prior art: drive photo evals), per docs/agents/llm-task-specs.md.""", [
    'Fake provider guesses become ranked Inbox guesses; guess 1 becomes the to-review Movement',
    'Provider error, timeout and invalid output each fall back without losing the Capture',
    'A photo from another chat or topic never reaches the task',
    'Photo sent is shrunk; thumbnails are only shortlisted Drives',
    'ADR 0009 amended; synthetic evals pass in CI',
], ['T4'])

ticket('T6', 't6-core', 'Custodian DM per Item, morning message, and the auto-approve switch', """\
The custodian gets one DM per Item and one morning message; a separate switch lets 0.95 guesses skip the DM.

- On a new Inbox item, the worker sends one DM per Item through the gateway's notify with: post link, photo, caption, up to 3 guesses (label, Project, last seen; exact guesses marked), and buttons New drive, Not a drive, Type it, None. The DM is edited to show the outcome after approval from the DM or the Inbox.
- **DM target** is configured: during Practice mode the DMs and bots test channel topic; after Cutover the custodian's private chat. The morning reminder (T6 Drives out more than 14 days) uses the same target and adds a list of unfinished Items (one message a day, never a separate nag).
- **`T6_DRIVE_AUTO_APPROVE`**: deployment setting, separate from `T6_DRIVE_LIVE`, off by default. When on, a 0.95 guess sends no DM but still waits in the Inbox for approval, pre-picked.
- Practice wording stays ("Practice").""", [
    'One DM per Item with the guesses and the five choices; tapping calls `drive approve`',
    'Approving in the Inbox edits the DM to the outcome',
    'Practice sends to the test channel topic; after Cutover to the private chat',
    'Morning message lists unfinished Items with the overdue T6 Drives, once a day',
    'Switch on: 0.95 guess sends no DM but is still in the Inbox; switch off: DM as usual',
    'Tested through the HTTP verb API and a fake gateway client (prior art: drive reminder tests)',
], ['T4'])

ticket('T7', 't6-core', 'Project drive movement reads for coordinators', """\
Coordinators can read their Projects' drive movements.

- A read of a Project's Movements: date, direction, Drive label, recipient or receiver, coordinator at the time, to-review flag, newest first.
- A read of weekly counts per Project (IN and OUT for a week) and the Drives of that Project out more than 14 days.
- Used by `/project` (gateway), the Drives app Project page and the Wednesday brief.""", [
    'Project Movements read returns the coordinator stored at the time of each move',
    'Weekly counts and the out-more-than-14-days list are right across a week boundary (Manila time)',
    'Tested through the HTTP verb API',
], ['T2'])

ticket('T8', 't6-core', 'Real-case eval for drive-photo-match and model choice', """\
Pick the vision model with real TRANSMITTAL photos.

- Ground truth for the 16 photos Cyril shared on 2026-11-25 (project files): Claude proposes each photo's Drive from the sheets, the custodian corrects in one pass. Kept off-repo.
- Run the eval on candidate image-capable models; choose the cheapest with guess 1 right at least 50% of the time. Record cost and latency per call.""", [
    'Ground truth confirmed by the custodian',
    'Eval results recorded for at least 3 models with accuracy, cost and latency',
    'Chosen model written in the deploy LLM config',
], ['T5'], label='ready-for-human')

ticket('T9', 't6-core', 'Live run recorded for drive-photo-match', """\
Record a live run of `drive-photo-match` before the release, per the evals spec #179 and docs/agents/llm-task-specs.md.""", [
    'Live run recorded with the chosen model and provider',
    'Release that ships the task is blocked until this is done',
], ['T5', 'T8'], label='ready-for-human')

ticket('T10', 't6-sixto-gateway', 'Quiet TRANSMITTAL group: reactions, scoped replies, undo with 🙈', """\
Sixto stays quiet in TRANSMITTAL and grades the poster's caption with reactions.

- After t6 confirms the capture write: react 👍 for grade `complete`, 🤔 for `thin` (same emoji as the watchdog).
- Text reply only for grade `problem`, naming the one missing thing (no IN/OUT, or an OUT without `R:`). An edit that fixes it re-reads the post and replaces the reply with a reaction.
- Remove the "Recorded" reply, the UNDO button and the label prompt.
- "undo" reply to the post from the poster or the custodian within the window: undo in t6, remove 👍/🤔 from the post, react 🙈 on the undo message. Others are ignored.
- Sixto is re-added to TRANSMITTAL only after this ships (rollout step).""", [
    '👍 and 🤔 follow the capture grade; no text on success',
    'Missing IN/OUT and missing `R:` each get one scoped reply; a fixing edit clears it',
    '"undo" within the window removes the reaction and reacts 🙈; after the window or from others it is ignored',
    'Tested with the existing fake Telegram and fake t6 API drive capture tests',
], ['T1'])

ticket('T11', 't6-sixto-gateway', 'Custodian DM buttons for Inbox approvals', """\
The custodian settles Items from her DM.

- Render t6's per-Item DM: photo, post link, guesses as buttons, plus New drive, Not a drive, Type it, None.
- Only the custodian may tap; others get a pop-up. A tap calls `drive approve`.
- "Type it": the next text reply to that DM is the Drive label or serial, sent as a typed approval.
- After approval (here or in the Inbox) the DM shows the outcome and the buttons go away.""", [
    'Each button maps to the right `drive approve` choice',
    'A tap by anyone other than the custodian changes nothing',
    'Type it accepts one reply and approves with it',
    'Tested with the fake Telegram and fake t6 API',
], ['T6'])

ticket('T12', 't6-sixto-gateway', '`/project` shows drive movements', """\
`/project` gains the Project's recent drive movements (date, IN/OUT, label, recipient or receiver, coordinator at the time) from the t6 read, on demand only; no pushed messages.""", [
    'Recent movements show on the Project card, newest first, with a cap and a "more in the Drives app" link',
    'Tested with the fake t6 API',
], ['T7'])

ticket('T13', 't6-drives', 'Inbox page for transmittal approvals', """\
A new **Inbox** page, first in the menu, lists Items waiting on approval: photo, caption, Item text, direction, recipient, guesses (exact ones marked) and the same choices as the DM (guess, New drive, Not a drive, Type it, None). Approving here updates the DM. Exceptions keeps sheet and data problems only. Plain words, no IDs on the page.""", [
    'Inbox lists waiting Items newest first and hides settled ones',
    'Each choice calls `drive approve` and shows the result',
    'Works on phone and computer widths',
    'Page tests against the fake API',
], ['T3'])

ticket('T14', 't6-drives', 'Project page shows drive movement history with the coordinator', """\
The Project page lists the Project's Movements with the coordinator at the time of each move, separately from the recipient, and marks to-review ones.""", [
    'History shows date, IN/OUT, label, recipient or receiver, coordinator, to-review mark',
    'Page tests against the fake API',
], ['T7'])

ticket('T15', 't6-notion-reports', 'Wednesday coordinator brief: drive movements per Project', """\
The Wednesday coordinator brief (and the master brief) adds, per Project, the week's movement count ("3 OUT, 1 IN") and a line for each Drive out more than 14 days, from the t6 read. Nothing when a Project had no movements.""", [
    'Counts and overdue lines appear under the right Project for the right coordinator',
    'Projects with no movements add nothing',
    'Tested with the existing Wednesday brief tests',
], ['T7'])


def gh(*args):
    return subprocess.run(['gh', *args], check=True, capture_output=True, text=True).stdout.strip()


def db_id(repo, number):
    return gh('api', f'repos/{OWNER}/{repo}/issues/{number}', '--jq', '.id')


def main():
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    labels = {}
    for t in T:
        if t['key'] in state:
            continue
        if (t['repo'], t['label']) not in labels:
            existing = gh('label', 'list', '--repo', f'{OWNER}/{t["repo"]}', '--limit', '200', '--json', 'name', '--jq', '.[].name').splitlines()
            if t['label'] not in existing:
                gh('label', 'create', t['label'], '--repo', f'{OWNER}/{t["repo"]}')
            labels[(t['repo'], t['label'])] = True
        blocked = '\n'.join(f'- {OWNER}/{state[k]["repo"]}#{state[k]["number"]}' for k in t['blocked']) or '- None (can start immediately)'
        body = (f'## Parent\n\n{SPEC}\n\n## What to build\n\n{t["build"]}\n\n## Acceptance criteria\n\n'
                + '\n'.join(f'- [ ] {c}' for c in t['criteria']) + f'\n\n## Blocked by\n\n{blocked}\n')
        url = gh('issue', 'create', '--repo', f'{OWNER}/{t["repo"]}', '--title', t['title'], '--label', t['label'], '--body', body)
        number = int(url.rstrip('/').rsplit('/', 1)[1])
        state[t['key']] = {'repo': t['repo'], 'number': number, 'url': url}
        STATE.write_text(json.dumps(state, indent=1))
        child = db_id(t['repo'], number)
        try:
            gh('api', '--method', 'POST', f'repos/{OWNER}/{PARENT[0]}/issues/{PARENT[1]}/sub_issues', '-F', f'sub_issue_id={child}')
        except subprocess.CalledProcessError as error:
            print('sub-issue link failed', t['key'], error.stderr)
        for k in t['blocked']:
            try:
                gh('api', '--method', 'POST', f'repos/{OWNER}/{t["repo"]}/issues/{number}/dependencies/blocked_by',
                   '-F', f'issue_id={db_id(state[k]["repo"], state[k]["number"])}')
            except subprocess.CalledProcessError as error:
                print('blocked-by link failed', t['key'], k, error.stderr)
        print(t['key'], url)
    print(json.dumps(state, indent=1))


if __name__ == '__main__':
    main()
