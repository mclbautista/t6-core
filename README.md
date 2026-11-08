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

Extras without an adapter yet are empty. Each adapter adds its dependencies to
its own extra only (`llm` adds pydantic). The `notion` extra does not install
Frame.io, Adobe sign-in, or LLM libraries.

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
from t6.llm import OpenAIChat, Ok, Task, run
from t6.privacy import pseudonymize

summarise = Task("summarise-v1", Notes, Summary, render)
masked, session = pseudonymize(text, registry)
provider = OpenAIChat(base_url="https://api.deepseek.com", api_key=key, model="deepseek-chat")

result = run(summarise, Notes(masked), provider=provider, session=session)
if isinstance(result, Ok):
    use(result.value)          # a Summary, with real names restored
else:
    log(result.reason)         # timeout, api_error, empty, invalid_json,
                               # schema_violation, unresolved_placeholder, leak
store(result.trace)            # prompt tag, model, tokens, latency in ms
```

`run` never raises for a bad reply. It parses the reply as JSON, validates it
against the output model, then runs `session.leaks()` and `session.restore()`.
A raw registry name in the reply fails with `leak`, and a placeholder the
session did not issue fails with `unresolved_placeholder`. Failure details
name field paths, never the names themselves. The trace holds no prompt or
reply text; the app decides where to store it.

Only programmer errors raise. Every string in the input must be `Masked`:
a plain `str` raises `UnmaskedInput` before anything is sent. Masked input
without its `Session` raises `MissingSession`. A `str` enum counts as a fixed
value, not free text.

`OpenAIChat` speaks the OpenAI-style `/chat/completions` API that DeepSeek
and OpenRouter serve, and asks for a JSON object reply by default
(`json_mode=False` turns that off).

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
fake.calls  # the messages each call sent, to check only masked text left
```

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
