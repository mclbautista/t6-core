"""Jev as a typed task: the Decisions API transport behind the same Task interface.

Task tests go through the scripted fake provider only. Transport tests stub
``send`` with hand-written JSON. No network, no recorded live replies.
"""

import dataclasses
import json
import unittest
import urllib.error
from email.message import Message as Headers
from typing import Any

from t6.llm import (
    CandidateDecisions,
    CandidateKeys,
    Choice,
    DataPolicy,
    DecisionsRequest,
    Failed,
    MissingSession,
    Ok,
    OpenAIChat,
    OpenRouterDecisions,
    ProviderError,
    ProviderTimeout,
    RateLimited,
    Task,
    Tokens,
    UnmaskedInput,
    candidate_key,
    run,
)
from t6.llm.testing import FakeProvider
from t6.privacy import Masked, Person, Project, Registry, Session, Withheld, pseudonymize, withhold

# The app owns the prompt: question wording and criteria live here, not in t6.
CRITERIA = {
    "match": "The Job Order is clearly work for this Deliverable.",
    "maybe": "The Job Order could be for this Deliverable, but the text doesn't make it clear.",
    "no": "The Job Order is not for this Deliverable.",
}


@dataclasses.dataclass(frozen=True)
class MatchInput:
    project: Masked
    job_order: Masked
    candidates: tuple[Masked, ...]  # Deliverable names, in CandidateKeys order. No page IDs.


def render(task_input: MatchInput) -> DecisionsRequest:
    return DecisionsRequest(
        state={"project": task_input.project.text, "job_order": task_input.job_order.text},
        questions={
            candidate_key(i): Choice(f"Does this Job Order belong to the Deliverable '{name.text}'?", CRITERIA)
            for i, name in enumerate(task_input.candidates)
        },
    )


MATCH: Task[MatchInput, CandidateDecisions] = Task("match-v1", MatchInput, CandidateDecisions, render)

PAGE_IDS = ["page-lb-ftr-scr", "page-mainpic-dcp"]


def registry() -> Registry:
    return Registry(people=[Person("Marco Rossi", aliases=["Marco"])], projects=[Project("Lantern Bay", code="LB")])


def masked_input() -> tuple[MatchInput, Session]:
    reg = registry()
    job_order, session = pseudonymize("LB JO #32: Marco exports Lantern Bay FTR with English subs", reg)
    return (
        MatchInput(
            project=session.mask("Lantern Bay"),
            job_order=job_order,
            candidates=(session.mask("Lantern Bay FTR SCR"), session.mask("Mainpic DCP")),
        ),
        session,
    )


def answer(choice: str = "match", match: float = 0.8, maybe: float = 0.15, no: float = 0.05) -> dict[str, Any]:
    return {"type": "choice", "choice": choice, "confidence": 0.7, "probabilities": {"match": match, "maybe": maybe, "no": no}}


def decisions_body(answers: object = None, **extra: object) -> dict[str, Any]:
    if answers is None:
        answers = {"c0": answer(), "c1": answer("no", 0.02, 0.08, 0.9)}
    return {
        "id": "gen-dec-example",
        "model": "typesafe/jev-1.1",
        "answers": answers,
        "usage": {"input_tokens": 210, "output_tokens": 14},
        **extra,
    }


class DecisionsTaskTests(unittest.TestCase):
    def test_decisions_task_returns_ok_with_per_candidate_probabilities(self) -> None:
        task_input, session = masked_input()
        fake = FakeProvider("~typesafe/jev-latest").decisions(decisions_body())

        result = run(MATCH, task_input, provider=fake, session=session)

        assert isinstance(result, Ok), result
        c0 = result.value.answers["c0"]
        self.assertEqual(c0.choice, "match")
        self.assertEqual((c0.probabilities.match, c0.probabilities.maybe, c0.probabilities.no), (0.8, 0.15, 0.05))
        self.assertEqual(result.value.answers["c1"].choice, "no")

    def test_app_maps_keys_back_to_page_ids(self) -> None:
        keys = CandidateKeys(PAGE_IDS)
        task_input, session = masked_input()
        result = run(MATCH, task_input, provider=FakeProvider().decisions(decisions_body()), session=session)

        assert isinstance(result, Ok)
        by_page = keys.by_page_id(result.value)
        self.assertEqual(list(by_page), PAGE_IDS)
        self.assertEqual(by_page["page-mainpic-dcp"].probabilities.no, 0.9)

    def test_only_candidate_keys_reach_the_transport(self) -> None:
        task_input, session = masked_input()
        fake = FakeProvider().decisions(decisions_body())
        run(MATCH, task_input, provider=fake, session=session)

        (call,) = fake.calls
        assert isinstance(call.request, DecisionsRequest)
        self.assertEqual(list(call.request.questions), ["c0", "c1"])
        sent = json.dumps(dataclasses.asdict(call.request))
        for page_id in PAGE_IDS:
            self.assertNotIn(page_id, sent)

    def test_only_masked_text_reaches_the_transport(self) -> None:
        task_input, session = masked_input()
        fake = FakeProvider().decisions(decisions_body())
        run(MATCH, task_input, provider=fake, session=session)

        request = fake.calls[0].request
        assert isinstance(request, DecisionsRequest)
        sent = json.dumps(dataclasses.asdict(request))
        for name in ("Lantern Bay", "Marco"):
            self.assertNotIn(name, sent)
        self.assertIn("[project 1]", sent)

    def test_trace_carries_model_tokens_latency_and_prompt_tag(self) -> None:
        task_input, session = masked_input()
        result = run(MATCH, task_input, provider=FakeProvider("~typesafe/jev-latest").decisions(decisions_body()), session=session)

        self.assertEqual(result.trace.prompt_tag, "match-v1")
        self.assertEqual(result.trace.model, "typesafe/jev-1.1")
        self.assertEqual(result.trace.tokens, Tokens(210, 14))
        self.assertGreaterEqual(result.trace.latency_ms, 0)


class AlphaShapeTests(unittest.TestCase):
    """The endpoint is alpha: anything off the documented shape fails closed as schema_violation."""

    def assert_schema_violation(self, body: object) -> Failed:
        task_input, session = masked_input()
        result = run(MATCH, task_input, provider=FakeProvider().decisions(body), session=session)
        assert isinstance(result, Failed), result
        self.assertEqual(result.reason, "schema_violation")
        return result

    def test_malformed_alpha_shapes_return_schema_violation(self) -> None:
        bad_probabilities = answer()
        bad_probabilities["probabilities"] = {"match": 0.8, "maybe": 0.15}
        echo = answer()
        echo["reason"] = "the job order names Lantern Bay"
        cases: dict[str, object] = {
            "not an object": ["c0"],
            "no model": {k: v for k, v in decisions_body().items() if k != "model"},
            "answers not an object": decisions_body(answers=[answer(), answer()]),
            "missing key": decisions_body(answers={"c0": answer()}),
            "unknown key": decisions_body(answers={"c0": answer(), "c1": answer(), "c2": answer()}),
            "wrong type": decisions_body(answers={"c0": {**answer(), "type": "score"}, "c1": answer()}),
            "unknown choice": decisions_body(answers={"c0": answer("yes"), "c1": answer()}),
            "missing probability": decisions_body(answers={"c0": bad_probabilities, "c1": answer()}),
            "out of range": decisions_body(answers={"c0": answer(match=1.2, maybe=-0.1, no=-0.1), "c1": answer()}),
            "does not sum to one": decisions_body(answers={"c0": answer(match=0.5, maybe=0.1, no=0.1), "c1": answer()}),
            "extra field echoes text": decisions_body(answers={"c0": echo, "c1": answer()}),
            "negative tokens": decisions_body(usage={"input_tokens": -1, "output_tokens": 0}),
        }
        for name, body in cases.items():
            with self.subTest(name):
                self.assert_schema_violation(body)

    def test_detail_names_paths_not_text(self) -> None:
        echo = answer()
        echo["reason"] = "the job order names Lantern Bay"
        failed = self.assert_schema_violation(decisions_body(answers={"c0": echo, "c1": answer()}))
        self.assertIn("answers.c0.reason", failed.detail)
        self.assertNotIn("Lantern Bay", failed.detail)

    def test_key_mismatch_detail_gives_counts(self) -> None:
        failed = self.assert_schema_violation(decisions_body(answers={"c0": answer(), "c7": answer()}))
        self.assertIn("1 missing and 1 unknown", failed.detail)

    def test_trace_keeps_returned_model_and_tokens_on_schema_violation(self) -> None:
        failed = self.assert_schema_violation(decisions_body(answers={"c0": answer()}))
        self.assertEqual(failed.trace.model, "typesafe/jev-1.1")
        self.assertEqual(failed.trace.tokens, Tokens(210, 14))

    def test_missing_usage_is_allowed(self) -> None:
        task_input, session = masked_input()
        body = {k: v for k, v in decisions_body().items() if k != "usage"}
        result = run(MATCH, task_input, provider=FakeProvider().decisions(body), session=session)
        assert isinstance(result, Ok)
        self.assertEqual(result.trace.tokens, Tokens())


class SameRulesAsChatTests(unittest.TestCase):
    """Masking, restore and trace run through the same ``run`` as chat tasks."""

    def test_unmasked_input_raises_and_sends_nothing(self) -> None:
        @dataclasses.dataclass(frozen=True)
        class Plain:
            project: str
            candidates: tuple[str, ...]

        def plain_render(task_input: Plain) -> DecisionsRequest:
            return DecisionsRequest({"project": task_input.project}, {})

        task: Task[Plain, CandidateDecisions] = Task("match-v1", Plain, CandidateDecisions, plain_render)
        fake = FakeProvider()
        with self.assertRaises(UnmaskedInput):
            run(task, Plain("Lantern Bay", ("page-mainpic-dcp",)), provider=fake)
        self.assertEqual(fake.calls, [])

    def test_masked_input_without_session_raises_and_sends_nothing(self) -> None:
        task_input, _ = masked_input()
        fake = FakeProvider().decisions(decisions_body())
        with self.assertRaises(MissingSession):
            run(MATCH, task_input, provider=fake)
        self.assertEqual(fake.calls, [])

    def test_transport_failures_map_like_chat(self) -> None:
        task_input, session = masked_input()
        fake = FakeProvider().timeout().api_error("HTTP 429")
        timeout = run(MATCH, task_input, provider=fake, session=session)
        api_error = run(MATCH, task_input, provider=fake, session=session)
        assert isinstance(timeout, Failed) and isinstance(api_error, Failed)
        self.assertEqual((timeout.reason, api_error.reason), ("timeout", "api_error"))
        self.assertEqual(timeout.trace.prompt_tag, "match-v1")

    def test_output_model_still_goes_through_leak_check_and_restore(self) -> None:
        class Explained(CandidateDecisions):
            # An app may widen the output model; the adapter checks it the same way.
            note: str = "[person 1] assigned"

        task_input, session = masked_input()
        task: Task[MatchInput, Explained] = Task("match-v1", MatchInput, Explained, render)
        result = run(task, task_input, provider=FakeProvider().decisions(decisions_body()), session=session)
        assert isinstance(result, Ok), result
        self.assertEqual(result.value.note, "Marco assigned")  # first casing seen in the input

    def test_provider_below_the_policy_returns_policy_and_sends_nothing(self) -> None:
        task_input, session = masked_input()
        fake = FakeProvider(retention="unknown").decisions(decisions_body())
        result = run(MATCH, task_input, provider=fake, session=session)
        assert isinstance(result, Failed), result
        self.assertEqual(result.reason, "policy")
        self.assertEqual(fake.calls, [])

    def test_decisions_transport_declares_unknown_retention_by_default(self) -> None:
        task_input, session = masked_input()
        send = Recorder(json.dumps(decisions_body()).encode())
        result = run(MATCH, task_input, provider=OpenRouterDecisions(api_key="k", send=send), session=session)
        assert isinstance(result, Failed)
        self.assertEqual(result.reason, "policy")
        self.assertEqual(send.requests, [])
        zdr = OpenRouterDecisions(api_key="k", retention="zdr_enforced", send=send)
        self.assertIsInstance(run(MATCH, task_input, provider=zdr, session=session), Ok)

    def test_withhold_policy_task_runs_without_a_session(self) -> None:
        @dataclasses.dataclass(frozen=True)
        class WithheldInput:
            job_order: Withheld
            candidates: tuple[Withheld, ...]

        def withheld_render(task_input: WithheldInput) -> DecisionsRequest:
            return DecisionsRequest(
                {"job_order": task_input.job_order.text},
                {candidate_key(i): Choice(c.text, CRITERIA) for i, c in enumerate(task_input.candidates)},
            )

        policy = DataPolicy(min_retention="unknown", masking="withhold")
        task: Task[WithheldInput, CandidateDecisions] = Task(
            "match-v1", WithheldInput, CandidateDecisions, withheld_render, policy=policy
        )
        payload = WithheldInput(withhold("JO #32 password: hunter2"), (withhold("FTR SCR"), withhold("DCP")))
        fake = FakeProvider(retention="unknown").decisions(decisions_body())
        result = run(task, payload, provider=fake)
        assert isinstance(result, Ok), result
        request = fake.calls[0].request
        assert isinstance(request, DecisionsRequest)
        self.assertNotIn("hunter2", json.dumps(dataclasses.asdict(request)))

    def test_one_429_is_waited_on_then_the_reply_is_used(self) -> None:
        task_input, session = masked_input()
        waits: list[float] = []
        fake = FakeProvider().rate_limited(retry_after=2).decisions(decisions_body())
        result = run(MATCH, task_input, provider=fake, session=session, sleep=waits.append)
        self.assertIsInstance(result, Ok)
        self.assertEqual(waits, [2])

    def test_a_chat_reply_cannot_answer_a_decisions_request(self) -> None:
        task_input, session = masked_input()
        with self.assertRaises(TypeError):
            run(MATCH, task_input, provider=FakeProvider().reply('{"answers": {}}'), session=session)

    def test_chat_transport_rejects_a_decisions_request(self) -> None:
        chat = OpenAIChat(base_url="https://llm.example", api_key="k", model="m", send=Recorder(b""))
        with self.assertRaises(TypeError):
            chat.complete(DecisionsRequest({}, {}), timeout=1)


class CandidateKeysTests(unittest.TestCase):
    def test_keys_follow_candidate_order(self) -> None:
        keys = CandidateKeys(["a", "b", "c"])
        self.assertEqual(keys.keys, ("c0", "c1", "c2"))
        self.assertEqual(keys.page_id("c1"), "b")
        self.assertEqual(len(keys), 3)

    def test_duplicate_page_ids_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CandidateKeys(["a", "a"])

    def test_unknown_key_raises(self) -> None:
        with self.assertRaises(KeyError):
            CandidateKeys(["a"]).page_id("c5")

    def test_by_page_id_needs_exactly_the_issued_keys(self) -> None:
        decisions = CandidateDecisions.model_validate(
            {"answers": {"c0": {"choice": "no", "probabilities": {"match": 0, "maybe": 0, "no": 1}}}}
        )
        with self.assertRaises(ValueError):
            CandidateKeys(["a", "b"]).by_page_id(decisions)


class Recorder:
    def __init__(self, response: bytes = b"", error: BaseException | None = None) -> None:
        self.response = response
        self.error = error
        self.requests: list[tuple[str, dict[str, str], dict[str, Any], float]] = []

    def __call__(self, url: str, headers: dict[str, str], body: bytes, timeout: float) -> bytes:
        self.requests.append((url, headers, json.loads(body), timeout))
        if self.error is not None:
            raise self.error
        return self.response


def two_questions() -> DecisionsRequest:
    return DecisionsRequest(
        state={"project": "[project 1]", "job_order": "masked message text"},
        questions={"c0": Choice("Does this Job Order belong to the Deliverable 'A'?", CRITERIA), "c1": Choice("B?", CRITERIA)},
    )


def transport(send: Recorder, **options: Any) -> OpenRouterDecisions:
    return OpenRouterDecisions(api_key="test-key", send=send, **options)


class OpenRouterDecisionsTests(unittest.TestCase):
    def test_sends_the_documented_request_body(self) -> None:
        send = Recorder(json.dumps(decisions_body()).encode())
        transport(send).complete(two_questions(), timeout=9)

        ((url, headers, payload, timeout),) = send.requests
        self.assertEqual(url, "https://openrouter.ai/api/alpha/decisions")
        self.assertEqual(headers["Authorization"], "Bearer test-key")
        self.assertEqual(timeout, 9)
        self.assertEqual(
            payload,
            {
                "model": "~typesafe/jev-latest",
                "state": {"project": "[project 1]", "job_order": "masked message text"},
                "questions": {
                    "c0": {
                        "type": "choice",
                        "instructions": "Does this Job Order belong to the Deliverable 'A'?",
                        "criteria": CRITERIA,
                    },
                    "c1": {"type": "choice", "instructions": "B?", "criteria": CRITERIA},
                },
            },
        )

    def test_reads_answers_model_and_tokens(self) -> None:
        completion = transport(Recorder(json.dumps(decisions_body()).encode())).complete(two_questions(), timeout=1)
        self.assertEqual(completion.model, "typesafe/jev-1.1")
        self.assertEqual(completion.tokens, Tokens(210, 14))
        assert completion.content is not None
        answers = json.loads(completion.content)["answers"]
        self.assertEqual(answers["c1"], {"choice": "no", "probabilities": {"match": 0.02, "maybe": 0.08, "no": 0.9}})

    def test_model_and_url_can_be_set(self) -> None:
        send = Recorder(json.dumps(decisions_body()).encode())
        provider = transport(send, model="typesafe/jev-1.1", url="https://router.example/decisions")
        provider.complete(two_questions(), timeout=1)
        self.assertEqual(provider.model, "typesafe/jev-1.1")
        self.assertEqual(send.requests[0][0], "https://router.example/decisions")
        self.assertEqual(send.requests[0][2]["model"], "typesafe/jev-1.1")

    def test_http_errors_raise_provider_error_without_the_body(self) -> None:
        error = urllib.error.HTTPError("https://openrouter.ai", 500, "boom", Headers(), None)
        with self.assertRaises(ProviderError) as caught:
            transport(Recorder(error=error)).complete(two_questions(), timeout=1)
        self.assertEqual(str(caught.exception), "HTTP 500")

    def test_429_raises_rate_limited_with_retry_after(self) -> None:
        headers = Headers()
        headers["Retry-After"] = "3"
        error = urllib.error.HTTPError("https://openrouter.ai", 429, "slow down", headers, None)
        with self.assertRaises(RateLimited) as caught:
            transport(Recorder(error=error)).complete(two_questions(), timeout=1)
        self.assertEqual(caught.exception.retry_after, 3)

    def test_timeouts_raise_provider_timeout(self) -> None:
        with self.assertRaises(ProviderTimeout):
            transport(Recorder(error=TimeoutError())).complete(two_questions(), timeout=1)

    def test_non_json_and_error_bodies_raise_provider_error(self) -> None:
        for raw in (b"<html>", json.dumps({"error": {"message": "x"}}).encode()):
            with self.subTest(raw=raw), self.assertRaises(ProviderError):
                transport(Recorder(raw)).complete(two_questions(), timeout=1)

    def test_rejects_chat_messages(self) -> None:
        with self.assertRaises(TypeError):
            transport(Recorder(b"")).complete([{"role": "user", "content": "hi"}], timeout=1)


if __name__ == "__main__":
    unittest.main()
