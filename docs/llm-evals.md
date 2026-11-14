# LLM eval runs: Hermes runbook

The app owns its prompt, JSON schema, cases, code grader and live-run entry
point. The t6 API owns the provider credential. Run this procedure on Sixto
through Hermes; do not put the API's provider key in the app or the runbook.
The app's live entry point should use `t6.llm.evals.run_live_evals` and
`Client.llm_run`, with a client timeout long enough for the API's LLM timeout.
The API must support `require_zdr` and return its ZDR attestation; upgrade it
before running this harness. A refusal or missing attestation stops the run.

1. Cyril initiates the run in Hermes on Sixto and selects the app's task and
   its app-owned prompt template and schema. The template must have one
   `{input}` marker. Configure the task name, masking preset, allowed model
   hint if needed, cost cap, and current prompt and completion token prices
   per token in the same currency as the cap. The cap is checked after each
   call, so one call can exceed it; set a cap with that in mind.
2. Set `T6_API_URL` and the authorized `T6_API_SECRET` for the t6 API; the
   API must have the LLM provider configured with zero retention. Configure
   `real_cases_path` to the private real-case JSON file on Sixto when running
   real cases. It is a JSON array of `Case` objects, with real provenance,
   reviewer and date; `masked_input` is a string. Keep real cases and their
   run records off-repo. The app supplies synthetic cases separately. All
   cases in a run must belong to the configured task and have unique IDs.
3. Run the app's live eval entry point. It passes the task definition to the
   API on each attempt and supplies the app's code grader and previous run
   history. Store one JSON record per run: in the app repo for synthetic
   cases, or in a private off-repo location for real cases. An incomplete run
   (including one stopped by the cap) is not release evidence. Investigate a
   refusal before rerunning; do not bypass the retention requirement.
4. Before release, review every case ID in `human_review`, including all failed
   and changed cases and up to five additional randomly sampled completed
   cases. `review_outputs` contains the three outputs for each sampled case;
   compare them with the corresponding case and expected properties. Record
   the reviewer's name in `reviewer` and the review date in `review_date` in
   the run JSON after review, and retain the record with the release evidence.
   Human review checks meaning and fluency; a model judge cannot decide pass
   or fail. Follow the runner's acceptance process for regressions.

Run the offline harness on every PR. Run live on any prompt, schema, model or
provider change, and before any release that changes an LLM task. Run real
cases monthly: a monthly reminder prompts Cyril to initiate the run and review
it. It is not an unattended job, because it spends money and needs a reviewer.
