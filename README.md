# t6-core

The shared T6 Python library. This repository was renamed from **t6-notion**
before its first commit. The distribution and import namespace are `t6`.

Requires Python 3.11 or newer. Install the base package with `pip install t6`.
The base package has no third-party runtime dependencies and includes
`t6.domain`, `t6.lookup`, and the `t6` command. The domain and lookup modules
are placeholders for their follow-up tickets.

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

All extras are declared and currently empty. Adapter dependencies will be added
when those adapters are implemented. The `notion` extra does not install
Frame.io, Adobe sign-in, or LLM libraries.

## CLI

```console
$ t6 version
{"version": "0.1.0"}
```

`python -m t6 version` provides the same output.

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
