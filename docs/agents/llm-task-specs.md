# LLM task specs and release tickets

Every new or changed LLM task must have an **Eval cases** section in its spec.
Include this section when using the spec-writing template:

## Eval cases

- **Task:** Identify the LLM task and what it is expected to do.
- **Expected properties:** Describe the observable properties outputs must have,
  including must-pass cases. Prefer properties over a single exact output.
- **Graders:** Say which properties code checks and which need human review.
  A model-as-judge may triage for human review, but cannot decide pass or fail.
- **Synthetic cases:** Describe the synthetic cases to commit in the app that
  owns the prompt, including cases for the expected properties.
- **Real cases:** State whether real cases exist and where they will be kept.
  They stay off-repo unless masked, checked against the names denylist, and
  reviewed by a person.

The task's tickets must include a **live run recorded** ticket that blocks
release of the task. Record a live run before any release that changes the
task, including prompt, schema, model, or provider changes. See
[evals spec #179](https://github.com/mclbautista/t6-core/issues/179) for the
run, review, and recording requirements.

Privacy is covered by contract tests, not evals: masking leak checks, restore,
and Unresolved fail-closed cases must pass on every PR.

`transcript translate` is an LLM task and follows this rule. Its spec also
describes masking for dialogue: character names are not registered names.
