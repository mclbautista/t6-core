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
