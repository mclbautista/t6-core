# t6-core

The shared T6 Python library. This repository was renamed from **t6-notion**
before its first commit. The distribution and import namespace are `t6`.

Requires Python 3.11 or newer. Install the base package with `pip install t6`.
The base package has no third-party runtime dependencies and includes
`t6.domain`, `t6.lookup`, and the `t6` command. The lookup module is a
placeholder for its follow-up ticket.

## Domain

`t6.domain` models T6 once, as immutable dataclasses that do no I/O:
Project, Episode, Deliverable, JobOrder, Contact, Studio, Machine (Bay),
Camera and ExternalRef.

- Each entity stored in Notion is identified by its Notion page ID. Two
  instances with the same page ID compare equal. Relations are page IDs.
- A Project's human key is its Project Code, which may be blank (`None`).
- `Project.episodes` derives Episodes 1 to `Eps` for the series Project Types
  (`SERIES_PROJECT_TYPES`). Films and other types have no Episodes.
- An ExternalRef (system, kind, external ID, URL) mirrors one External Links
  row. System, kind and external ID identify the object; the URL may change.

### Events and jobs

Tool events and the work they trigger share one typed shape, used by the
webhook server, the worker and poll-and-diff.

- `FrameioCommentCreated` (Frame.io comment ID, Frame.io project ID, optional
  file ID) and `ProjectStatusChanged` (Project page ID, new Status) are Events.
- Each Event has a `dedupe_key` built from the tool's own IDs. The same tool
  event has the same key whether it came from a webhook or from poll-and-diff;
  `source` and `received_at` are carried but are not part of the key.
- A `Job` carries its `JobKind`, a read-only JSON-ready payload and a dedupe
  key. `Job.for_event(kind, event)` keys the job by kind and event, so one
  event queues each kind of job at most once.

## Optional extras

Install only the adapters an application needs, for example
`pip install 't6[notion]'`.

| Extra | Purpose |
| --- | --- |
| `notion` | Notion client and models |
| `privacy` | Withholding and reversible masking |
| `llm` | Typed LLM tasks and transports |
| `frameio` | Frame.io integration |
| `resolve` | DaVinci Resolve integration |
| `instagantt` | Instagantt export reader |
| `server` | Webhook server and queue |
| `worker` | Job workers |

The `notion` extra installs `httpx` and the `llm` extra installs `pydantic`.
The other extras are declared and currently empty; their dependencies will be
added when those adapters are implemented. The `notion` extra does not install Frame.io, Adobe sign-in, or
LLM libraries.

## Notion

`t6.notion` is the one shared Notion client. Install it with
`pip install 't6[notion]'`.

- `AsyncNotionClient` is a generic async httpx client: `retrieve_page`,
  `create_page`, `update_page`, `retrieve_data_source`, and
  `query_data_source` (which follows `next_cursor` and returns every row) or
  `iter_data_source` (which yields rows page by page).
- `NotionClient` is a thin blocking wrapper with the same methods and the same
  results. It runs its own event loop, so do not call it from async code.
- Every request sends Notion-Version `NOTION_VERSION` (`2026-03-11`). Pass
  `notion_version=` to one client to override it for that client only.
- Every failure raises `NotionError` with `status`, `code`, `message` and
  `retry_after`. A timeout or a network error has `status` None and `code`
  `timeout` or `network_error`.
- `t6.notion.codec` decodes property values. `t6.notion.models` turns pages
  into `t6.domain` entities; `project_from_page` builds a Project from a
  Projects page (property names checked against the live schema).
- `t6.notion.testing.FakeNotion` is an in-memory Notion behind an httpx mock
  transport, with `fail_next` and `time_out_next` to inject errors.

```python
from t6.notion import NotionClient, RetryPolicy
from t6.notion.models import project_from_page

with NotionClient(token, retry=RetryPolicy()) as notion:
    project = project_from_page(notion.retrieve_page(page_id))
    rows = notion.query_data_source(projects_data_source_id)
```

### Retry policy

This policy is written in ASD-STE100 Simplified Technical English.

1. Retry is off by default. To turn it on, give a `RetryPolicy` to the client.
2. When retry is off, the client does not retry. Each error causes a
   `NotionError` immediately.
3. A read is a page retrieve, a data source retrieve, or a data source query.
   A write is a page create or a page update.
4. When Notion sends status 429, the client retries the call. This rule
   applies to reads and to writes.
5. When Notion sends a status of 500 or more, the client retries a read. It
   does not retry a write.
6. When a read times out, the client retries the read. When a write times
   out, the client does not retry the write.
7. Other errors cause a `NotionError` immediately. Examples are status 400,
   401 and 404.
8. If a write gets a status of 500 or more, or times out, the client raises
   `NotionError`. The write possibly occurred. Do not send the write again
   before you make sure that it did not occur.
9. Before each retry, the client waits. If Notion sends a Retry-After value,
   the client waits for that number of seconds.
10. If Notion does not send a Retry-After value, the client waits
    `base_delay` seconds (default 0.5). After each retry, the wait becomes
    two times longer. The wait does not become longer than `max_delay`
    seconds (default 8).
11. The client tries a call a maximum of `attempts` times (default 3). This
    number includes the first try. After the last try, the client raises the
    last `NotionError`.

## Privacy

`t6.privacy.withhold(text)` strips labelled credentials (`pw`, `pass`,
`password`, `passcode`, `pin` followed directly by `:` or `=`) before text
leaves T6. It is one way: the result holds only the masked `text`, the `count`
of values withheld, and the `masker_version`, so nothing can be restored. URLs
are kept byte for byte. It is ported from the watchdog masker in t6-jo-tg-ntn
and needs no third-party packages or Notion access.

```python
from t6.privacy import withhold

result = withhold("PW: secret")
result.text            # 'PW: [password withheld – see Telegram message]'
result.count           # 1
result.masker_version  # '1.0.0'
```

`t6.privacy.pseudonymize(text, registry, allow)` swaps registry names for
placeholders before text leaves T6, and its `Session` puts them back
afterwards. It uses only the standard library.

```python
from t6.privacy import Person, Project, Registry, pseudonymize

registry = Registry(
    people=[Person("Marco Rossi", aliases=["Marco", "Rossi"], handles=["@marcor"])],
    projects=[Project("Other Show", code="OS1")],
    ids=["123456789"],
)
masked, session = pseudonymize("Marco confirmed Other Show", registry)
# masked.text == "[person 1] confirmed [project 1]"
leaks = session.leaks(reply)      # raw registry names in the reply
restored = session.restore(reply) # raises Unresolved if a stray placeholder is left
```

Matching is by whole word and ignores case. Names in `allow` stay unmasked.
`restore` walks dataclasses, pydantic models, dicts, lists and tuples, accepts
placeholder variants such as `[PERSON 1 ]` or `person 1`, and restores only
placeholders the session issued. Any other placeholder-shaped token raises
`Unresolved` and nothing is restored.

## LLM

`t6.llm` runs typed LLM tasks. Install it with `pip install 't6[llm]'`
(pydantic v2 is its only dependency). A `Task` has a prompt tag, an input
model and a pydantic output model. Prompts stay in the app: the task's
`render` function turns the input into chat messages.

```python
from t6.llm import DataPolicy, LLMConfig, Ok, Task, run
from t6.privacy import pseudonymize

summarise = Task("summarise-v1", Notes, Summary, render, repair_quotes=True)
masked, session = pseudonymize(text, registry)
provider = LLMConfig.from_mapping(app_config["llm"]).provider_for("summarise-v1")

result = run(summarise, Notes(masked), provider=provider, session=session)
if isinstance(result, Ok):
    use(result.value)          # a Summary, with real names restored
else:
    log(result.reason)         # policy, timeout, api_error, empty, invalid_json,
                               # schema_violation, unresolved_placeholder, leak
store(result.trace)            # prompt tag, model, tokens, latency in ms, repaired
```

`run` never raises for a bad reply. It strips a Markdown code fence if the
whole reply is wrapped in one, parses the reply as JSON, validates it
against the output model, then runs `session.leaks()` and `session.restore()`.
A raw registry name in the reply fails with `leak`, and a placeholder the
session did not issue fails with `unresolved_placeholder`. Failure details
name field paths, never the names themselves. The trace holds no prompt or
reply text; the app decides where to store it.

Only programmer errors raise. Every text value in the input must be masked
the way the task's policy says (below): a plain `str`, or text masked the
other way, raises `UnmaskedInput` before anything is sent. Masked input
without its `Session` raises `MissingSession`. A `str` enum counts as a fixed
value, not free text.

### Data policy

Each provider declares its retention: `zdr_enforced` (zero data retention is
enforced on every request) or `unknown`. Each task declares a `DataPolicy`:
the minimum retention it accepts and the masking its input must carry.

| Masking | Every text value in the input must be | Session |
| --- | --- | --- |
| `pseudonymize` | `t6.privacy.Masked` (names swapped, restored after) | required |
| `withhold` | `t6.privacy.Withheld` (credentials removed, names kept) | not needed |

```python
report = Task("report-v1", Notes, Summary, render)   # default: zdr_enforced + pseudonymize
classify = Task("classify-v1", Ticket, Label, render_ticket, policy=DataPolicy("unknown", "withhold"))
```

If the provider's retention is below the task's minimum, `run` returns
`Failed("policy")` and sends nothing.

### Provider config

Provider choice and retention live in the app's config, not in code. The app
parses its own config file and passes the `llm` section as a mapping:

```yaml
llm:
  default: deepseek
  providers:
    deepseek:
      base_url: https://api.deepseek.com
      model: deepseek-chat
      retention: unknown
      api_key_env: DEEPSEEK_API_KEY
    openrouter:
      base_url: https://openrouter.ai/api/v1
      model: some/model
      retention: zdr_enforced
      api_key_env: OPENROUTER_API_KEY
  tasks:
    report-v1: openrouter
```

`LLMConfig.from_mapping(...)` checks the section and raises `ConfigError` for
a missing `retention`, an unknown provider name or an unknown key. API keys
never go in config: `provider_for(prompt_tag)` reads the key from the
environment variable each provider names. A task with no `tasks` entry uses
`default`.

### JSON replies and retries

A reply wrapped in a code fence is always unwrapped. Quote repair (escaping
double quotes left unescaped inside a string value) is opt-in per task with
`repair_quotes=True`. It runs only when the reply fails to parse, before
validation and restore, and the trace then shows `repaired=True`. There is no
other repair and no model repair call: anything else is `invalid_json`.

On a 429 `run` waits once, for the `Retry-After` the provider sent (one
second if none, `api_error` at once if it asks for more than 60), then tries
again. A second 429 returns `Failed("api_error")`. Callers own every other
retry.

`OpenAIChat` speaks the OpenAI-style `/chat/completions` API that DeepSeek
and OpenRouter serve, and asks for a JSON object reply by default
(`json_mode=False` turns that off).

### Jev: Decisions tasks

Jev runs as a normal typed task on OpenRouter's Decisions API. Its `render`
returns a `DecisionsRequest` instead of chat messages: masked `state`, and one
`Choice` question per candidate keyed `c0`, `c1`, ... The output model is
`CandidateDecisions`: a choice (`match`, `maybe` or `no`) and the three
probabilities per key. `CandidateKeys` stays in the app and maps the keys
back to Notion page IDs, so a page ID never reaches the transport.

```python
from t6.llm import (CandidateDecisions, CandidateKeys, Choice, DecisionsRequest, Ok,
                    OpenRouterDecisions, Task, candidate_key, run)

def render(job: MatchInput) -> DecisionsRequest:
    return DecisionsRequest(
        state={"project": job.project.text, "job_order": job.job_order.text},
        questions={candidate_key(i): Choice(f"Does this Job Order belong to '{name.text}'?", CRITERIA)
                   for i, name in enumerate(job.candidates)},
    )

match = Task("match-v1", MatchInput, CandidateDecisions, render)
keys = CandidateKeys([d.page_id for d in deliverables])   # same order as job.candidates
result = run(match, job, provider=OpenRouterDecisions(api_key=key), session=session)
if isinstance(result, Ok):
    by_page = keys.by_page_id(result.value)               # page ID -> CandidateDecision
```

Use `session.mask(text)` to mask each extra field (project name, Deliverable
names) with the same session, so placeholders stay consistent.

The endpoint is alpha, so the transport checks every reply against the
documented shape: `model`, one `choice` answer per question key and no other
keys, probabilities from 0 to 1 that sum to 1, no extra fields such as a
reason. Anything else fails with `schema_violation`, and the detail names
field paths only. Data policy, masking, `leaks()`, `restore()`, the one 429
wait and the trace work the same as for chat tasks. `OpenRouterDecisions`
declares `retention="unknown"` unless told otherwise, so give a Jev task a
`DataPolicy` it can meet, or declare `zdr_enforced` when the account enforces it.

### Testing

Tests use `t6.llm.testing.FakeProvider`, never a live provider or recorded
replies (they can hold real names). Queue steps in order, then run tasks:

```python
from t6.llm.testing import FakeProvider

fake = FakeProvider("deepseek-chat")
fake.reply('{"headline": "[PERSON 1 ] moved the grade"}', prompt_tokens=40, completion_tokens=8)
fake.reply("not json")
fake.timeout()
fake.api_error("HTTP 503")
fake.rate_limited(retry_after=2)
fake.decisions({"model": "typesafe/jev-1.1", "answers": {...}})  # a raw Decisions body
fake.calls  # the request each call sent, to check only masked text left
```

A fake declares `zdr_enforced` retention; `FakeProvider(retention="unknown")`
tests the policy check. `run(..., sleep=...)` takes a stand-in for
`time.sleep` so 429 tests do not wait. `fake.decisions(body)` runs the body
through the same alpha-shape validation as `OpenRouterDecisions`, so a
malformed body fails with `schema_violation`.

## CLI

```console
$ t6 version
{"version": "0.1.0"}
```

`python -m t6 version` provides the same output.

## This repository is public

Anything pushed here is visible to everyone at once, and Docker images install
`t6` from it with no GitHub token. Never commit:

- passwords, API keys, tokens or private keys, even in a test or a comment
- a `.env` file (`.env.example` with empty values is fine)
- a real `config.yaml` (commit `config.example.yaml` instead)
- databases (`*.db`, `*.sqlite`), anything in a `data/` folder, or token store
  files such as `token.json`
- real client, show or person names, or any client data, in code, tests,
  fixtures or docs; use made-up names

If a secret is ever pushed, rotate it straight away. Deleting the commit is not
enough, because the old value is already public.

### Safety checks

The `Public repo` workflow runs on every push and pull request. It fails when
gitleaks finds a likely secret in the new commits, when a tracked file has a
forbidden path, or when a tracked line contains a name from the denylist. The
denylist lives only in the `T6_NAMES_DENYLIST` Actions secret (one name per
line), and the check prints the file and line, never the name. The same job
installs `t6[notion]` from this repo with no credentials.

Run the same checks locally before pushing (needs
[gitleaks](https://github.com/gitleaks/gitleaks#installing) on your `PATH`):

```sh
T6_NAMES_DENYLIST="$(cat ~/.t6-names)" python3 scripts/public_repo_safety.py
```

Add `--full-history` to scan every commit, or `--skip-secrets` to run only the
path and names checks. Other public T6 repositories turn the checks on by
calling the reusable workflow; see the header of
`.github/workflows/public-repo-safety.yml`.

## Development

This is a single-package uv workspace. Install uv, then run:

```sh
uv sync --locked
uv run --locked mypy
uv run --locked python -m unittest discover -s tests -v
uv build
```

The tests exercise the public CLI and build a wheel, then install the base
package and the Notion extra into separate clean environments without an
index. CI runs these checks on push and pull request for Python 3.11–3.14.
