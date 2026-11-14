"""t6.llm end to end through the scripted fake provider. No live provider, no recorded replies."""

import dataclasses
import json
import unittest
from enum import Enum

from pydantic import BaseModel

from t6.llm import Failed, Message, MissingSession, Ok, Task, Tokens, UnmaskedInput, run
from t6.llm.testing import FakeProvider
from t6.privacy import Masked, Person, Project, Registry, Session, pseudonymize


class Tone(str, Enum):
    brief = "brief"
    full = "full"


@dataclasses.dataclass(frozen=True)
class Notes:
    text: Masked
    tone: Tone = Tone.brief
    max_items: int = 3


class Change(BaseModel):
    who: str
    what: str


class Summary(BaseModel):
    headline: str
    changes: list[Change]


def render(notes: Notes) -> list[Message]:
    # The prompt lives in the app; the adapter only sees messages.
    return [
        {"role": "system", "content": f"Summarise in JSON. Tone: {notes.tone.value}."},
        {"role": "user", "content": notes.text.text},
    ]


SUMMARISE: Task[Notes, Summary] = Task("summarise-v1", Notes, Summary, render)


def registry() -> Registry:
    return Registry(people=[Person("Marco Rossi", aliases=["Marco"])], projects=[Project("Other Show", code="OS1")])


def masked_notes() -> tuple[Notes, Session]:
    masked, session = pseudonymize("Marco Rossi moved the Other Show grade to Friday", registry())
    return Notes(masked), session


def reply(headline: str, who: str = "[person 1]", what: str = "moved the grade") -> str:
    return json.dumps({"headline": headline, "changes": [{"who": who, "what": what}]})


class HappyPathTests(unittest.TestCase):
    def test_pseudonymized_input_returns_ok_with_real_names_restored(self) -> None:
        masked, session = pseudonymize("Marco Rossi moved the Other Show grade to Friday", registry())
        fake = FakeProvider("deepseek-chat")
        fake.reply(reply("[PERSON 1 ] moved [project 1]"), prompt_tokens=42, completion_tokens=9)

        result = run(SUMMARISE, Notes(masked), provider=fake, session=session)

        assert isinstance(result, Ok)
        self.assertEqual(result.value.headline, "Marco Rossi moved Other Show")
        self.assertEqual(result.value.changes, [Change(who="Marco Rossi", what="moved the grade")])
        self.assertTrue(result.ok)

    def test_only_masked_text_reaches_the_provider(self) -> None:
        masked, session = pseudonymize("Marco Rossi moved the Other Show grade to Friday", registry())
        fake = FakeProvider().reply(reply("ok"))
        run(SUMMARISE, Notes(masked), provider=fake, session=session, timeout=5)

        (call,) = fake.calls
        sent = " ".join(message["content"] for message in call.messages)
        self.assertNotIn("Marco", sent)
        self.assertNotIn("Other Show", sent)
        self.assertIn("[person 1]", sent)
        self.assertEqual(call.timeout, 5)

    def test_input_without_any_text_needs_no_session(self) -> None:
        @dataclasses.dataclass(frozen=True)
        class Counts:
            jobs: int

        class Verdict(BaseModel):
            busy: bool

        task: Task[Counts, Verdict] = Task(
            "busy-v1", Counts, Verdict, lambda c: [{"role": "user", "content": str(c.jobs)}]
        )
        result = run(task, Counts(4), provider=FakeProvider().reply('{"busy": true}'))
        assert isinstance(result, Ok)
        self.assertTrue(result.value.busy)


class ProgrammerErrorTests(unittest.TestCase):
    def test_unmasked_input_raises(self) -> None:
        @dataclasses.dataclass(frozen=True)
        class RawNotes:
            text: str

        task: Task[RawNotes, Summary] = Task(
            "raw-v1", RawNotes, Summary, lambda n: [{"role": "user", "content": n.text}]
        )
        fake = FakeProvider().reply(reply("x"))
        with self.assertRaises(UnmaskedInput) as caught:
            run(task, RawNotes("Marco Rossi moved the grade"), provider=fake)
        self.assertIn("text", str(caught.exception))
        self.assertNotIn("Marco", str(caught.exception))
        self.assertEqual(fake.calls, [])

    def test_plain_text_nested_beside_masked_text_raises(self) -> None:
        @dataclasses.dataclass(frozen=True)
        class Mixed:
            body: Masked
            extra: dict[str, list[str]]

        masked, session = pseudonymize("Marco", registry())
        task: Task[Mixed, Summary] = Task("mixed-v1", Mixed, Summary, lambda m: [])
        with self.assertRaises(UnmaskedInput) as caught:
            run(task, Mixed(masked, {"lines": ["raw"]}), provider=FakeProvider(), session=session)
        self.assertIn("extra.lines[0]", str(caught.exception))

    def test_pseudonymized_input_without_session_raises(self) -> None:
        notes, _ = masked_notes()
        fake = FakeProvider().reply(reply("x"))
        with self.assertRaises(MissingSession):
            run(SUMMARISE, notes, provider=fake)
        self.assertEqual(fake.calls, [])

    def test_wrong_input_type_raises(self) -> None:
        with self.assertRaises(TypeError):
            run(SUMMARISE, object(), provider=FakeProvider())  # type: ignore[misc]


class FailureTests(unittest.TestCase):
    def run_scripted(self, fake: FakeProvider) -> Failed:
        notes, session = masked_notes()
        result = run(SUMMARISE, notes, provider=fake, session=session)
        assert isinstance(result, Failed), result
        self.assertFalse(result.ok)
        return result

    def test_timeout(self) -> None:
        self.assertEqual(self.run_scripted(FakeProvider().timeout()).reason, "timeout")

    def test_api_error(self) -> None:
        self.assertEqual(self.run_scripted(FakeProvider().api_error("HTTP 503")).reason, "api_error")

    def test_empty(self) -> None:
        for content in (None, "", "   \n"):
            with self.subTest(content=content):
                self.assertEqual(self.run_scripted(FakeProvider().reply(content)).reason, "empty")

    def test_invalid_json(self) -> None:
        for content in ('{"headline": "x",', "Sure! Here is the summary.", "{'headline': 'x'}"):
            with self.subTest(content=content):
                self.assertEqual(self.run_scripted(FakeProvider().reply(content)).reason, "invalid_json")

    def test_schema_violation(self) -> None:
        for content in ('{"headline": "x"}', '{"headline": 3, "changes": []}', "[]"):
            with self.subTest(content=content):
                failed = self.run_scripted(FakeProvider().reply(content))
                self.assertEqual(failed.reason, "schema_violation")

    def test_unresolved_placeholder(self) -> None:
        failed = self.run_scripted(FakeProvider().reply(reply("[person 2] moved [project 1]")))
        self.assertEqual(failed.reason, "unresolved_placeholder")
        self.assertIn("person 2", failed.detail)
        self.assertFalse(hasattr(failed, "value"))
        self.assertNotIn("Other Show", repr(failed))

    def test_leak(self) -> None:
        failed = self.run_scripted(FakeProvider().reply(reply("ok", who="Marco")))
        self.assertEqual(failed.reason, "leak")
        self.assertIn("changes[0].who", failed.detail)
        self.assertNotIn("Marco", failed.detail)


class TraceTests(unittest.TestCase):
    def test_every_result_has_a_full_trace(self) -> None:
        scripts = {
            "ok": FakeProvider("m").reply(reply("[person 1]"), prompt_tokens=10, completion_tokens=4),
            "timeout": FakeProvider("m").timeout(),
            "api_error": FakeProvider("m").api_error(),
            "empty": FakeProvider("m").reply(""),
            "invalid_json": FakeProvider("m").reply("{", prompt_tokens=10, completion_tokens=1),
            "schema_violation": FakeProvider("m").reply("{}"),
            "unresolved_placeholder": FakeProvider("m").reply(reply("[id 9]")),
            "leak": FakeProvider("m").reply(reply("Other Show")),
        }
        for expected, fake in scripts.items():
            with self.subTest(expected=expected):
                notes, session = masked_notes()
                result = run(SUMMARISE, notes, provider=fake, session=session)
                self.assertEqual("ok" if isinstance(result, Ok) else result.reason, expected)
                trace = result.trace
                self.assertEqual(trace.prompt_tag, "summarise-v1")
                self.assertEqual(trace.model, "m")
                self.assertIsInstance(trace.tokens, Tokens)
                self.assertGreaterEqual(trace.latency_ms, 0)

    def test_trace_carries_reported_tokens_and_model(self) -> None:
        notes, session = masked_notes()
        fake = FakeProvider("router-default").reply(
            reply("[person 1]"), prompt_tokens=120, completion_tokens=30, model="deepseek/deepseek-chat"
        )
        result = run(SUMMARISE, notes, provider=fake, session=session)
        self.assertEqual(result.trace.model, "deepseek/deepseek-chat")
        self.assertEqual(result.trace.tokens, Tokens(120, 30))
        self.assertEqual(result.trace.tokens.total, 150)

    def test_failure_before_reply_traces_the_configured_model_and_zero_tokens(self) -> None:
        notes, session = masked_notes()
        result = run(SUMMARISE, notes, provider=FakeProvider("cfg").timeout(), session=session)
        self.assertEqual(result.trace.model, "cfg")
        self.assertEqual(result.trace.tokens, Tokens())


class FakeProviderTests(unittest.TestCase):
    def test_running_out_of_script_is_a_test_error(self) -> None:
        notes, session = masked_notes()
        with self.assertRaises(AssertionError):
            run(SUMMARISE, notes, provider=FakeProvider(), session=session)

    def test_steps_replay_in_order(self) -> None:
        fake = FakeProvider().timeout().reply(reply("[person 1]"))
        notes, session = masked_notes()
        first = run(SUMMARISE, notes, provider=fake, session=session)
        second = run(SUMMARISE, notes, provider=fake, session=session)
        self.assertIsInstance(first, Failed)
        self.assertIsInstance(second, Ok)
        self.assertEqual(fake.pending, 0)


if __name__ == "__main__":
    unittest.main()
