"""#7: data policy, provider config, fence stripping, quote repair and the one 429 wait.

Every task test runs through the scripted fake provider. No live provider, no recorded replies.
"""

import dataclasses
import io
import json
import unittest
import urllib.error
from email.message import Message as Headers

from pydantic import BaseModel

from t6.llm import (
    ConfigError,
    DataPolicy,
    Failed,
    LLMConfig,
    Message,
    Ok,
    OpenAIChat,
    RateLimited,
    Task,
    UnmaskedInput,
    run,
)
from t6.llm.jsontext import repair_quotes, strip_fence
from t6.llm.testing import FakeProvider
from t6.llm.transport import parse_retry_after
from t6.privacy import Masked, Person, Registry, Session, Withheld, pseudonymize, withhold


@dataclasses.dataclass(frozen=True)
class Notes:
    text: Masked


@dataclasses.dataclass(frozen=True)
class Ticket:
    text: Withheld


class Summary(BaseModel):
    headline: str


class Label(BaseModel):
    label: str


def render_notes(notes: Notes) -> list[Message]:
    return [{"role": "user", "content": notes.text.text}]


def render_ticket(ticket: Ticket) -> list[Message]:
    return [{"role": "user", "content": ticket.text.text}]


REPORT = Task("report-v1", Notes, Summary, render_notes, policy=DataPolicy("zdr_enforced", "pseudonymize"))
REPORT_REPAIRING = dataclasses.replace(REPORT, repair_quotes=True)
CLASSIFY = Task("classify-v1", Ticket, Label, render_ticket, policy=DataPolicy("unknown", "withhold"))


def notes() -> tuple[Notes, Session]:
    masked, session = pseudonymize("Marco Rossi moved the grade", Registry(people=[Person("Marco Rossi")]))
    return Notes(masked), session


def ticket() -> Ticket:
    return Ticket(withhold("Marco Rossi says the NAS pin: 4321 stopped working"))


class Sleeps:
    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


class DataPolicyTest(unittest.TestCase):
    def test_zdr_pseudonymize_task_on_unknown_provider_fails_policy_without_a_call(self) -> None:
        fake = FakeProvider("deepseek-chat", retention="unknown")
        fake.reply('{"headline": "never sent"}')
        payload, session = notes()

        result = run(REPORT, payload, provider=fake, session=session)

        assert isinstance(result, Failed)
        self.assertEqual(result.reason, "policy")
        self.assertIn("unknown", result.detail)
        self.assertEqual(fake.calls, [])
        self.assertEqual(fake.pending, 1)
        self.assertEqual(result.trace.prompt_tag, "report-v1")
        self.assertEqual(result.trace.model, "deepseek-chat")
        self.assertEqual(result.trace.tokens.total, 0)

    def test_zdr_provider_meets_a_zdr_task(self) -> None:
        fake = FakeProvider(retention="zdr_enforced").reply('{"headline": "[person 1] moved it"}')
        payload, session = notes()
        result = run(REPORT, payload, provider=fake, session=session)
        assert isinstance(result, Ok)
        self.assertEqual(result.value.headline, "Marco Rossi moved it")

    def test_run_tells_the_provider_to_ask_for_zdr_only_on_zdr_tasks(self) -> None:
        fake = FakeProvider().reply('{"headline": "[person 1] moved it"}').reply('{"label": "it"}')
        payload, session = notes()
        run(REPORT, payload, provider=fake, session=session)
        run(CLASSIFY, ticket(), provider=fake)
        self.assertEqual([call.zdr for call in fake.calls], [True, False])

    def test_zdr_provider_also_meets_a_task_that_accepts_unknown(self) -> None:
        fake = FakeProvider(retention="zdr_enforced").reply('{"label": "it"}')
        self.assertIsInstance(run(CLASSIFY, ticket(), provider=fake), Ok)

    def test_withhold_task_runs_on_an_unknown_provider_with_no_session(self) -> None:
        fake = FakeProvider(retention="unknown").reply('{"label": "it"}')
        result = run(CLASSIFY, ticket(), provider=fake)
        assert isinstance(result, Ok)
        sent = fake.calls[0].messages[0]["content"]
        self.assertNotIn("4321", sent)

    def test_default_policy_is_the_strictest(self) -> None:
        self.assertEqual(DataPolicy(), DataPolicy("zdr_enforced", "pseudonymize"))
        task = Task("t", Notes, Summary, render_notes)
        self.assertEqual(task.policy, DataPolicy())
        self.assertFalse(task.repair_quotes)

    def test_withheld_text_in_a_pseudonymize_task_raises_before_the_policy_check(self) -> None:
        @dataclasses.dataclass(frozen=True)
        class Wrong:
            text: Withheld

        task = Task("report-v1", Wrong, Summary, lambda w: [{"role": "user", "content": w.text.text}])
        fake = FakeProvider()
        with self.assertRaises(UnmaskedInput) as caught:
            run(task, Wrong(withhold("pin: 1234")), provider=fake)
        self.assertIn("text", str(caught.exception))
        self.assertIn("pseudonymize", str(caught.exception))
        self.assertEqual(fake.calls, [])

    def test_masked_or_plain_text_in_a_withhold_task_raises(self) -> None:
        @dataclasses.dataclass(frozen=True)
        class Mixed:
            a: object
            b: object

        task = Task("classify-v1", Mixed, Label, lambda m: [], policy=DataPolicy("unknown", "withhold"))
        masked, _ = pseudonymize("x", Registry())
        with self.assertRaises(UnmaskedInput) as caught:
            run(task, Mixed(masked, "plain"), provider=FakeProvider())
        self.assertIn("a, b", str(caught.exception))
        self.assertIn("withhold", str(caught.exception))

    def test_policy_rejects_unknown_values(self) -> None:
        with self.assertRaises(ValueError):
            DataPolicy("forever")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            DataPolicy("unknown", "encrypt")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            OpenAIChat(base_url="https://example.invalid", api_key="k", model="m", retention="zdr")  # type: ignore[arg-type]

    def test_openai_chat_retention_defaults_to_unknown(self) -> None:
        self.assertEqual(OpenAIChat(base_url="https://example.invalid", api_key="k", model="m").retention, "unknown")


def config(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "default": "cheap",
        "providers": {
            "cheap": {
                "base_url": "https://cheap.example.invalid",
                "model": "cheap-chat",
                "retention": "unknown",
                "api_key_env": "CHEAP_KEY",
            },
            "private": {
                "base_url": "https://private.example.invalid/api/v1/",
                "model": "private/model",
                "retention": "zdr_enforced",
                "api_key_env": "PRIVATE_KEY",
                "json_mode": False,
            },
        },
        "tasks": {"report-v1": "private"},
    }
    data.update(overrides)
    return data


ENV = {"CHEAP_KEY": "test-cheap", "PRIVATE_KEY": "test-private"}


class ConfigTest(unittest.TestCase):
    def test_provider_choice_and_retention_come_from_config(self) -> None:
        cfg = LLMConfig.from_mapping(config())

        report = cfg.provider_for("report-v1", env=ENV)
        other = cfg.provider_for("classify-v1", env=ENV)

        self.assertEqual((report.model, report.retention), ("private/model", "zdr_enforced"))
        self.assertEqual((other.model, other.retention), ("cheap-chat", "unknown"))

    def test_config_drives_the_policy_decision(self) -> None:
        cfg = LLMConfig.from_mapping(config(tasks={"report-v1": "cheap"}))
        provider = cfg.provider_for("report-v1", env=ENV)
        payload, session = notes()
        result = run(REPORT, payload, provider=provider, session=session)
        assert isinstance(result, Failed)
        self.assertEqual(result.reason, "policy")

    def test_built_provider_uses_the_configured_endpoint_key_and_json_mode(self) -> None:
        sent: list[tuple[str, dict[str, str], bytes]] = []

        def send(url: str, headers: dict[str, str], body: bytes, timeout: float) -> bytes:
            sent.append((url, headers, body))
            return json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode()

        provider = LLMConfig.from_mapping(config()).provider_for("report-v1", env=ENV, send=send)
        provider.complete([{"role": "user", "content": "hi"}], timeout=5)

        url, headers, body = sent[0]
        self.assertEqual(url, "https://private.example.invalid/api/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer test-private")
        self.assertNotIn("response_format", json.loads(body))

    def test_no_task_entry_and_no_default_is_a_config_error(self) -> None:
        cfg = LLMConfig.from_mapping(config(default=None))
        with self.assertRaises(ConfigError):
            cfg.provider_for("classify-v1", env=ENV)

    def test_missing_key_variable_is_a_config_error(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            LLMConfig.from_mapping(config()).provider_for("report-v1", env={})
        self.assertIn("PRIVATE_KEY", str(caught.exception))

    def test_invalid_configs(self) -> None:
        good = config()["providers"]
        assert isinstance(good, dict)
        cases: dict[str, dict[str, object]] = {
            "no providers": config(providers={}),
            "missing retention": config(providers={"p": {k: v for k, v in good["cheap"].items() if k != "retention"}}),
            "bad retention": config(providers={"p": {**good["cheap"], "retention": "zdr"}}),
            "api key in config": config(providers={"p": {**good["cheap"], "api_key": "x"}}),
            "empty model": config(providers={"p": {**good["cheap"], "model": " "}}),
            "json_mode not bool": config(providers={"p": {**good["cheap"], "json_mode": "yes"}}),
            "unknown task provider": config(tasks={"report-v1": "nope"}),
            "unknown default": config(default="nope"),
            "unknown top key": config(retries=3),
        }
        for name, data in cases.items():
            with self.subTest(name), self.assertRaises(ConfigError):
                LLMConfig.from_mapping(data)

    def test_api_key_in_config_names_the_fix(self) -> None:
        good = config()["providers"]
        assert isinstance(good, dict)
        with self.assertRaises(ConfigError) as caught:
            LLMConfig.from_mapping(config(providers={"p": {**good["cheap"], "api_key": "x"}}))
        self.assertIn("api_key_env", str(caught.exception))


class JsonHardeningTest(unittest.TestCase):
    def test_fenced_json_parses_without_opting_in(self) -> None:
        for reply in (
            '```json\n{"headline": "[person 1] moved it"}\n```',
            '```\n{"headline": "[person 1] moved it"}\n```',
            '  ```JSON {"headline": "[person 1] moved it"} ```  \n',
        ):
            with self.subTest(reply):
                payload, session = notes()
                result = run(REPORT, payload, provider=FakeProvider().reply(reply), session=session)
                assert isinstance(result, Ok)
                self.assertEqual(result.value.headline, "Marco Rossi moved it")
                self.assertFalse(result.trace.repaired)

    def test_broken_quotes_fail_without_opting_in(self) -> None:
        payload, session = notes()
        broken = '{"headline": "[person 1] said "ship it" today"}'
        result = run(REPORT, payload, provider=FakeProvider().reply(broken), session=session)
        assert isinstance(result, Failed)
        self.assertEqual(result.reason, "invalid_json")
        self.assertFalse(result.trace.repaired)

    def test_broken_quotes_parse_when_the_task_opts_in_and_the_trace_says_repaired(self) -> None:
        payload, session = notes()
        broken = '```json\n{"headline": "[person 1] said "ship it" today"}\n```'
        result = run(REPORT_REPAIRING, payload, provider=FakeProvider().reply(broken), session=session)
        assert isinstance(result, Ok)
        self.assertEqual(result.value.headline, 'Marco Rossi said "ship it" today')
        self.assertTrue(result.trace.repaired)

    def test_repair_runs_before_restore_so_a_placeholder_inside_the_quote_is_restored(self) -> None:
        payload, session = notes()
        broken = '{"headline": "the note reads "[person 1] approved""}'
        result = run(REPORT_REPAIRING, payload, provider=FakeProvider().reply(broken), session=session)
        assert isinstance(result, Ok)
        self.assertEqual(result.value.headline, 'the note reads "Marco Rossi approved"')

    def test_valid_json_is_not_marked_repaired_on_an_opted_in_task(self) -> None:
        payload, session = notes()
        result = run(REPORT_REPAIRING, payload, provider=FakeProvider().reply('{"headline": "ok"}'), session=session)
        assert isinstance(result, Ok)
        self.assertFalse(result.trace.repaired)

    def test_repair_does_not_rescue_other_damage(self) -> None:
        payload, session = notes()
        result = run(REPORT_REPAIRING, payload, provider=FakeProvider().reply('{"headline": "cut off'), session=session)
        assert isinstance(result, Failed)
        self.assertEqual(result.reason, "invalid_json")
        self.assertIn("quote repair", result.detail)
        self.assertFalse(result.trace.repaired)

    def test_strip_fence_leaves_unfenced_and_partly_fenced_text_alone(self) -> None:
        self.assertEqual(strip_fence('{"a": 1}'), '{"a": 1}')
        prose = 'Here you go:\n```json\n{"a": 1}\n```'
        self.assertEqual(strip_fence(prose), prose)

    def test_repair_quotes_keeps_valid_escapes_and_escapes_raw_newlines(self) -> None:
        self.assertEqual(json.loads(repair_quotes('{"a": "x \\" y"}')), {"a": 'x " y'})
        self.assertEqual(json.loads(repair_quotes('{"a": "line\nnext"}')), {"a": "line\nnext"})


class RateLimitTest(unittest.TestCase):
    def test_one_429_with_retry_after_waits_once_then_retries(self) -> None:
        fake = FakeProvider().rate_limited(retry_after=3).reply('{"headline": "[person 1] moved it"}')
        sleeps = Sleeps()
        payload, session = notes()

        result = run(REPORT, payload, provider=fake, session=session, sleep=sleeps)

        assert isinstance(result, Ok)
        self.assertEqual(sleeps.waits, [3])
        self.assertEqual(len(fake.calls), 2)

    def test_a_second_429_returns_api_error(self) -> None:
        fake = FakeProvider().rate_limited(retry_after=2).rate_limited(retry_after=2).reply('{"headline": "x"}')
        sleeps = Sleeps()
        payload, session = notes()

        result = run(REPORT, payload, provider=fake, session=session, sleep=sleeps)

        assert isinstance(result, Failed)
        self.assertEqual(result.reason, "api_error")
        self.assertIn("429", result.detail)
        self.assertEqual(sleeps.waits, [2])
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(fake.pending, 1)

    def test_429_without_retry_after_waits_the_default(self) -> None:
        fake = FakeProvider().rate_limited().reply('{"headline": "x"}')
        sleeps = Sleeps()
        payload, session = notes()
        self.assertIsInstance(run(REPORT, payload, provider=fake, session=session, sleep=sleeps), Ok)
        self.assertEqual(sleeps.waits, [1.0])

    def test_429_asking_for_too_long_a_wait_fails_at_once(self) -> None:
        fake = FakeProvider().rate_limited(retry_after=600).reply('{"headline": "x"}')
        sleeps = Sleeps()
        payload, session = notes()
        result = run(REPORT, payload, provider=fake, session=session, sleep=sleeps)
        assert isinstance(result, Failed)
        self.assertEqual(result.reason, "api_error")
        self.assertEqual(sleeps.waits, [])
        self.assertEqual(len(fake.calls), 1)

    def test_other_api_errors_are_not_retried(self) -> None:
        fake = FakeProvider().api_error("HTTP 503").reply('{"headline": "x"}')
        sleeps = Sleeps()
        payload, session = notes()
        result = run(REPORT, payload, provider=fake, session=session, sleep=sleeps)
        assert isinstance(result, Failed)
        self.assertEqual(result.reason, "api_error")
        self.assertEqual((sleeps.waits, len(fake.calls)), ([], 1))

    def test_transport_maps_http_429_and_its_retry_after(self) -> None:
        def send(url: str, headers: dict[str, str], body: bytes, timeout: float) -> bytes:
            response_headers = Headers()
            response_headers["Retry-After"] = "7"
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", response_headers, io.BytesIO(b"echo"))

        provider = OpenAIChat(base_url="https://example.invalid", api_key="k", model="m", send=send)
        with self.assertRaises(RateLimited) as caught:
            provider.complete([{"role": "user", "content": "hi"}], timeout=5)
        self.assertEqual(caught.exception.retry_after, 7.0)

    def test_parse_retry_after(self) -> None:
        self.assertEqual(parse_retry_after("5"), 5.0)
        self.assertEqual(parse_retry_after(" 1.5 "), 1.5)
        self.assertIsNone(parse_retry_after(None))
        self.assertIsNone(parse_retry_after("soon"))
        self.assertEqual(parse_retry_after("-3"), 0.0)
        # An HTTP date 10 seconds after "now".
        self.assertEqual(parse_retry_after("Thu, 01 Jan 1970 00:00:10 GMT", now=0.0), 10.0)


if __name__ == "__main__":
    unittest.main()
