# Hand-over: state on 2026-11-17 and where the next round starts

This page is for the person or agent who picks the work up after a pause. It says what is live,
which rules were decided, and which issue to open for each piece of open work. It names no
person, client or show: "the owner" is the studio owner who decides and approves.

Read this page first, then the spec you will work on, then its comments. The comments of #426
hold every owner decision of the last round.

## What is live

| Part | Version | Where |
|---|---|---|
| t6 API, main worker, Frame.io worker | `v0.6.8` (schema 45) | the studio server |
| t6 edge (webhooks, queue, taps) | `v0.6.8` | the VPS |
| Reports (`t6-notion-reports`) | main at `b45f1d2` | the studio server |
| Gateway (`t6-sixto-gateway`) | main at `f4ce92b` | the studio server |
| Watchdog (`t6-jo-watchdog`) | image from `11cf91e`, with its own Notion and LLM keys. The switch to the API image (`10399de`) on 2026-11-17 was undone the same day: see #498 | the studio server |
| Munki fleet tools (`t6-munki-repo`) | checkout at `bdac236`, no service | the operator Mac |
| Drive web app (`t6-drives`) | pinned at t6 `v0.6.2` | the studio server |
| Transcribe worker | paused by the owner on 2026-11-16 | not running |
| Bay calendars (Google) | five calendars, five channels, three editors, since 2026-11-17 | the studio server (API config) and the VPS (webhook route) |

The deploy pin and the runbooks for the hosts are in the private repository `t6-netops`.
`t6-munki-repo` is private since 2026-11-17, because its data files are read by the tools and
cannot hold placeholders. Links from the other repositories to its issues show only to people
with access.

Three host facts to know before any work on the hosts (read on 2026-11-17):

- After a reboot or a power cut nothing comes back by itself. Auto-login is off and nothing
  starts the container runtime, so a person (or an SSH session) must start it. The fix and the
  restart test are in mclbautista/t6-netops#71.
- The deploy account on the studio server has no sudo. A restart of the machine needs the owner.
- On the VPS, name the compose file in every command (`docker compose -f compose.yaml ...`). On
  2026-11-17 a plain `docker compose up -d server` read a forgotten override file and ran the
  0.3.11 image for 87 minutes. The file is renamed now. The record and the open follow-ups are in
  mclbautista/t6-netops#135.

The watchdog is the one app that is not on the API on this date. Every extraction call of the API
image was refused by `llm run` (rule `masking`), because the prompt and the schema hold words that
are also registered Project names or codes. The check of the switch compared stored rows and made
no LLM call, so it did not see this. A switch of an app that calls the LLM needs one live call
through the API first. The fix is #498 and mclbautista/t6-jo-watchdog#159.

## Rules that were decided (do not reopen without the owner)

1. **Apps are clients of the API.** An app imports only `t6.client` and `t6.verbs`
   ([ADR 0002](adr/0002-consumer-contract.md); `t6 admin consumer-check` enforces it). An app
   holds no Notion and no LLM credential. Every read and write goes through the API.
2. **Apps change in logic, not in rules.** A rule (a name, a number, a status, a match, a
   deadline, who is cc'd) lives in the API. When an app needs a rule the API does not have,
   add a verb; do not copy the rule into the app.
3. **Matching a name is one API call** (`person find`, `project find`). No app keeps matching code.
4. **A Job Order with no Project is allowed.** It is raised and listed as an exception.
5. **Statuses stay as they are in Notion.** The API never makes a new Status option.
6. **One Concern list, in the API.** The weekly run closes no Concern. A close that comes from a
   written line is announced first. The old reports store was not imported.
7. **LLM calls go through `t6.llm` with zero data retention.** There are two written exceptions,
   set in the API's provider config: the watchdog's extraction provider, and the Decisions
   provider (Deliverable matching and the message filter). Both get masked input.
8. **Less caution for changes.** Only two things are in real daily use by the team: the Job
   Order rows the watchdog writes, and the shift reports. So: no week-long shadow runs. For a
   switch, one comparison of old against new, a backup, a one-line rollback, and the owner's
   yes for each tag and deploy.
9. **Production deploys from the repository**, never from edited files on a host.
10. **Releases:** a release pull request, then the tag and the GitHub release, then the pin in
    `t6-netops`. A cloud session cannot push a tag; the tag is pushed from the operator Mac.

## How the work is done

Grill, spec, tickets, implement, code review, as separate stages. A spec is an issue; its
tickets are sub-issues. Labels: `needs-triage`, `needs-info`, `ready-for-agent`,
`ready-for-human`, `wontfix` (see `docs/agents/`).

Checks before a pull request in this repository: `uv sync --locked`, `uv run --locked mypy`,
`uv run --locked lint-imports`, `uv run --locked deptry src`,
`uv run --locked python -m unittest discover -s tests`.

## Open work

### 1. Built, waits for a live step by a person (`ready-for-human`)

| Issue | What is needed |
|---|---|
| #250 | Concern matching live run: the owner's go, a cost cap, real cases, the owner as reviewer. |
| #253 | abang live check: the owner and one freelancer in one real group. |
| #276 | Bay calendars are live (#259, #260, #270, #271 closed). Checks 1 to 3 passed on 2026-11-17. Open: check 4 (a person with view access only tries to change a booking), and the recheck on 2026-11-21 that the five channels were renewed. Each editor adds the calendars to their own list once (`docs/deployment.md`). |
| #263, #55 | Drive import: the drive custodian clears the exceptions and signs off. |
| #226 | First recorded live PSR eval run. |
| #213, #214 | Transcripts: live run and acceptance. They wait for the transcribe worker (paused). |
| #279 | The live record of release 0.6.0 (bookkeeping). |
| #374, #375 | Bay scanner fact check and its ADR: one bay to inspect, and the owner's yes. |
| mclbautista/t6-sixto-gateway#97 | Telegram transport roll-out for Chat links and abang. |
| mclbautista/t6-netops#71 | The restart check after a power cut. The owner restarts the studio server; the session records what comes back. |
| mclbautista/t6-jo-watchdog#103, #426 | The switch was undone on 2026-11-17 and waits for #498 and mclbautista/t6-jo-watchdog#159. After the fix: one live extraction through the API, a copy of the ledger, the owner's yes, then the switch. Both close when the first real Job Order message is seen written by the API image. |

### 2. Ready for an agent (`ready-for-agent`)

| Issue | Note |
|---|---|
| #498, mclbautista/t6-jo-watchdog#159 | **First in line.** `llm run` refuses a prompt or schema with an ordinary word that is also a registered Project name or code; the watchdog prompt also holds a registered example code and has no start check. A patch release (0.6.9), then the second switch of the watchdog. |
| #376, #377, #379, #380, #381, #274 | Bay scanner (#18) and Timeline conflicts. All wait for the fact check #374. |
| mclbautista/t6-munki-repo#20 | Munki recipe for the bay scan job. Waits for #18. |
| #414 | A failed job can be dismissed (0.6.8) but not run again. |
| mclbautista/t6-jo-watchdog#158 | A refused API read is held like a timeout; `compare-raise` says why a row was not built. |
| mclbautista/t6-netops#133 | 78 failed jobs in the VPS queue: read them, then dismiss. |
| mclbautista/t6-netops#135 | After the old image on the VPS (2026-11-17): the runbook names the compose file, a check compares the running image with the pin, and 9 refused Frame.io events are looked up. |
| #453 | Message filter for each Telegram chat and topic. Built; the count-first step and the switch are open. |
| #362 | Refactor backlog from the Phase 3 review. Triage each line first. |
| mclbautista/t6-sixto-gateway#87, #104 | Chat links transport; a DM reply to the PSR brief passed through to t6. |
| mclbautista/t6-notion-reports#112, #113 | Frame.io activity and Instagantt dates in the briefs (features, parked by the owner). |
| mclbautista/t6-netops#132, #72, #105, #107 | Nightly network scan; off-site backups and restore drill; transcribe worker; two live checks. |

### 3. Needs a grill or triage before any code (`needs-triage`)

| Issue | Question |
|---|---|
| #24 | Public planner CSV endpoints. The owner said yes on 2026-11-17; the ticket needs a rewrite for the API as it is now. |
| #28 | Fleet Orchestrator. The owner said yes on 2026-11-17: it can run on the operator Mac and needs a complete SSH table. Needs a grill. |
| #493 | `project list` is slow on the live system, and `project resolve` and `person find` still read live Notion (calls of 12 to 18 seconds were seen). Measure first. |
| #494 | Small gaps left after 0.6.8 (review leftovers, `jo raise` gaps). |
| #491 | Leftovers of the 0.6.8 docs check. |
| mclbautista/t6-notion-reports#134 | Project status rules still in the reports app. |
| mclbautista/t6-sixto-gateway#118 | The base compose file publishes the port on every interface. |
| mclbautista/t6-sixto-gateway#53, #54 | `/jo` formats a Job Order (feature, parked). |
| mclbautista/t6-munki-repo#3, #7, #9, #18 | Bays write-back and the Resolve scan job. The 0.6.8 preview would write nothing; see #7. |
| mclbautista/t6-drives#19 | Inventory page: first read is slow. Waits for #493, then the pin moves from `v0.6.2`. |
| mclbautista/agent-skills#64 | The watchdog folder on the hosts still has the old name; one test fails because of it. |

### 4. Specs that stay open as the map

#178 (the shape of t6-core; read its state block first), #179 (LLM evals), #180 (Transcripts),
#115 (consumer contract), #426 (apps onto the API), mclbautista/t6-netops#1 and #64.

## Staff documents (drafts in the studio wiki, Status "For approval")

| Document | For | State on 2026-11-17 |
|---|---|---|
| Operator manual | the operator and the maintainer | At 0.6.8. The watchdog rows describe the API image; correct them after the second switch. |
| New Employee Handbook (nine chapters) | all staff | At 0.6.8. Three owner questions are open. |
| Traffic Coordinator Manual (six chapters) | the standing cc of every Job Order | Written on 2026-11-17 and checked against the repositories. Six owner questions are open: what the role may do (attach a Project, link a Frame.io project, hold `bookings`), if it reads the operations topic, where staff ask for a Booking, and if Project Start and the PSR briefing belong in it. |

None is published. The owner approves each one. The links and the open questions are in the
operator's private notes.

## Where to read more

- Verbs and what each returns: `README.md` and `docs/api-read-verbs.md`.
- Deploy, configuration and the request log: `docs/deployment.md`, and `t6-netops` for the hosts.
- LLM evals and how a live run is started: `docs/llm-evals.md`.
- Words of the domain: `CONTEXT.md`. Decisions: `docs/adr/`.
- What changed in each release: `CHANGELOG.md`.

## First steps for a new session

1. Check what is live: `t6 health`, and the pin in `t6-netops`.
2. Check what `main` holds after the last tag (`git log v0.6.8..main`). On 2026-11-17 it was
   docs only.
3. Check which watchdog image runs. If it is still `11cf91e`, #498 is not live yet.
4. Open the issue for the work, read its comments, and check its label.
5. Ask the owner only what the issues do not answer, and ask in one batch.
