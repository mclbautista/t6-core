"""OpenAIChat request and response mapping, with a stubbed send. No network, no recorded replies."""

import json
import socket
import unittest
import urllib.error
from email.message import Message as Headers

from t6.llm import (
    Choice,
    ConfigError,
    DecisionsRequest,
    Image,
    LLMConfig,
    Message,
    OpenAIChat,
    OpenRouterDecisions,
    ProviderError,
    ProviderTimeout,
    ScriptPhase,
    ScriptRun,
    Tokens,
    ToolCall,
    ToolSpec,
    Trace,
)
from t6.llm.testing import FakeProvider


class Recorder:
    def __init__(self, response: bytes = b"", error: BaseException | None = None) -> None:
        self.response = response
        self.error = error
        self.requests: list[tuple[str, dict[str, str], dict[str, object], float]] = []

    def __call__(self, url: str, headers: dict[str, str], body: bytes, timeout: float) -> bytes:
        self.requests.append((url, headers, json.loads(body), timeout))
        if self.error is not None:
            raise self.error
        return self.response


def body(content: object = '{"a": 1}', **extra: object) -> bytes:
    return json.dumps(
        {
            "model": "deepseek-chat",
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
            **extra,
        }
    ).encode()


def provider(send: Recorder, **options: object) -> OpenAIChat:
    return OpenAIChat(base_url="https://llm.example/v1/", api_key="test-key", model="deepseek-chat", send=send, **options)  # type: ignore[arg-type]


class OpenAIChatTests(unittest.TestCase):
    def test_image_capability_can_be_configured_and_requires_a_boolean(self) -> None:
        entry = {"base_url": "https://llm.example/v1", "api_key_env": "TEST_KEY",
                 "model": "test-vision", "retention": "zdr_enforced", "supports_images": True}
        config = {"default": "vision", "providers": {"vision": entry}}
        send = Recorder(body())
        chat = LLMConfig.from_mapping(config).provider_for("drive-photo-read-v1", env={"TEST_KEY": "k"}, send=send)
        chat.complete([{"role": "user", "content": "Read", "image": Image(b"synthetic")}], timeout=1)
        self.assertEqual(len(send.requests), 1)
        for invalid in ("yes", 1, None):
            with self.subTest(invalid=invalid), self.assertRaises(ConfigError):
                LLMConfig.from_mapping({"providers": {"vision": {**entry, "supports_images": invalid}}})

    def test_refuses_image_before_sending_when_support_is_not_declared(self) -> None:
        send = Recorder(body())
        with self.assertRaisesRegex(ProviderError, "does not support image input"):
            provider(send).complete(
                [{"role": "user", "content": "Read", "image": Image(b"synthetic")}], timeout=1,
            )
        self.assertEqual(send.requests, [])

    def test_sends_image_part_with_text_for_image_capable_provider(self) -> None:
        send = Recorder(body())
        provider(send, supports_images=True).complete(
            [{"role": "user", "content": "Read the drive", "image": Image(b"synthetic")}], timeout=7,
        )
        self.assertEqual(send.requests[0][2]["messages"], [{
            "role": "user", "content": [
                {"type": "text", "text": "Read the drive"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,c3ludGhldGlj"}},
            ],
        }])

    def test_sends_an_openai_style_chat_request(self) -> None:
        send = Recorder(body())
        provider(send).complete([{"role": "user", "content": "[person 1]"}], timeout=7)
        ((url, headers, payload, timeout),) = send.requests
        self.assertEqual(url, "https://llm.example/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer test-key")
        self.assertEqual(payload["model"], "deepseek-chat")
        self.assertEqual(payload["messages"], [{"role": "user", "content": "[person 1]"}])
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(timeout, 7)

    def test_zdr_task_on_openrouter_asks_for_zero_data_retention(self) -> None:
        send = Recorder(body())
        chat = OpenAIChat(base_url="https://openrouter.ai/api/v1", api_key="k", model="m", send=send)
        chat.complete([], timeout=1, zdr=True)
        self.assertEqual(send.requests[0][2]["provider"], {"zdr": True})

    def test_no_zdr_flag_without_a_zdr_task_or_on_another_provider(self) -> None:
        send = Recorder(body())
        OpenAIChat(base_url="https://openrouter.ai/api/v1", api_key="k", model="m", send=send).complete([], timeout=1)
        provider(send).complete([], timeout=1, zdr=True)
        self.assertNotIn("provider", send.requests[0][2])
        self.assertNotIn("provider", send.requests[1][2])

    def test_json_mode_can_be_turned_off(self) -> None:
        send = Recorder(body())
        provider(send, json_mode=False).complete([], timeout=1)
        self.assertNotIn("response_format", send.requests[0][2])

    def test_reads_content_model_and_tokens(self) -> None:
        completion = provider(Recorder(body())).complete([], timeout=1)
        self.assertEqual(completion.content, '{"a": 1}')
        self.assertEqual(completion.model, "deepseek-chat")
        self.assertEqual(completion.tokens, Tokens(12, 3))

    def test_null_content_is_kept_as_none(self) -> None:
        self.assertIsNone(provider(Recorder(body(None))).complete([], timeout=1).content)

    def test_timeouts_raise_provider_timeout(self) -> None:
        for error in (TimeoutError(), socket.timeout(), urllib.error.URLError(TimeoutError())):
            with self.subTest(error=error), self.assertRaises(ProviderTimeout):
                provider(Recorder(error=error)).complete([], timeout=1)

    def test_http_errors_raise_provider_error_without_the_body(self) -> None:
        error = urllib.error.HTTPError("https://llm.example", 500, "boom", Headers(), None)
        self.addCleanup(error.close)  # an unclosed HTTPError warns when collected, which fails other tests on 3.14
        with self.assertRaises(ProviderError) as caught:
            provider(Recorder(error=error)).complete([], timeout=1)
        self.assertEqual(str(caught.exception), "HTTP 500")

    def test_connection_errors_raise_provider_error(self) -> None:
        for error in (urllib.error.URLError(ConnectionRefusedError()), ConnectionResetError()):
            with self.subTest(error=error), self.assertRaises(ProviderError):
                provider(Recorder(error=error)).complete([], timeout=1)

    def test_unreadable_responses_raise_provider_error(self) -> None:
        for raw in (b"<html>", b"{}", json.dumps({"choices": []}).encode(), body(content=5), body(error={"message": "x"})):
            with self.subTest(raw=raw), self.assertRaises(ProviderError):
                provider(Recorder(raw)).complete([], timeout=1)


class MissingUsageTests(unittest.TestCase):
    def test_missing_usage_is_unknown(self) -> None:
        from t6.llm.transport import _completion
        result = _completion(b'{"choices":[{"message":{"content":"{}"}}]}', 'model')
        self.assertIsNone(result.tokens.prompt)
        self.assertIsNone(result.tokens.completion)
        self.assertIsNone(result.tokens.total)

    def test_partial_usage_is_unknown(self) -> None:
        from t6.llm.transport import _completion
        result = _completion(b'{"choices":[{"message":{"content":"{}"}}],"usage":{"prompt_tokens":0}}', 'model')
        self.assertEqual(result.tokens.prompt, 0)
        self.assertIsNone(result.tokens.completion)


class ToolCallingTransportTests(unittest.TestCase):
    def test_openai_chat_serializes_tools_and_omits_when_empty(self) -> None:
        send = Recorder(body())
        spec = ToolSpec(
            name="lookup_person",
            description="Look up person metadata.",
            parameters={"type": "object", "properties": {"who": {"type": "string"}}, "required": ["who"]},
        )
        chat = provider(send)
        chat.complete([{"role": "user", "content": "[person 1]"}], timeout=5, tools=[spec])
        chat.complete([{"role": "user", "content": "plain"}], timeout=5)

        first_payload = send.requests[0][2]
        second_payload = send.requests[1][2]
        self.assertEqual(
            first_payload["tools"],
            [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup_person",
                        "description": "Look up person metadata.",
                        "parameters": {
                            "type": "object",
                            "properties": {"who": {"type": "string"}},
                            "required": ["who"],
                        },
                    },
                }
            ],
        )
        self.assertNotIn("tools", second_payload)

    def test_openai_chat_sends_assistant_tool_calls_and_tool_role_messages(self) -> None:
        send = Recorder(body())
        messages: list[Message] = [
            {"role": "user", "content": "[person 1]"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": '{"who": "[person 1]"}'}}
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": '{"role": "editor"}'},
        ]
        provider(send).complete(messages, timeout=3)
        self.assertEqual(send.requests[0][2]["messages"], messages)

    def test_openai_chat_parses_tool_calls(self) -> None:
        raw = json.dumps(
            {
                "model": "deepseek-chat",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "lookup", "arguments": '{"who": "[person 1]"}'},
                                },
                                {
                                    "id": "call_2",
                                    "type": "function",
                                    "function": {"name": "ping", "arguments": ""},
                                },
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 6},
            }
        ).encode()
        completion = provider(Recorder(raw)).complete([], timeout=1)
        self.assertIsNone(completion.content)
        self.assertEqual(
            completion.tool_calls,
            (
                ToolCall(id="call_1", name="lookup", arguments={"who": "[person 1]"}),
                ToolCall(id="call_2", name="ping", arguments={}),
            ),
        )

    def test_openai_chat_rejects_malformed_tool_calls(self) -> None:
        bad_tool_calls: list[object] = [
            "not-a-list",
            ["not-a-dict"],
            [{"id": "c1", "type": "code_interpreter", "function": {"name": "f", "arguments": "{}"}}],
            [{"id": "", "type": "function", "function": {"name": "f", "arguments": "{}"}}],
            [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{bad"}}],
            [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "[1, 2]"}}],
            [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": 5}}],
        ]
        for bad in bad_tool_calls:
            raw = json.dumps(
                {"model": "m", "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": bad}}]}
            ).encode()
            with self.subTest(bad=bad), self.assertRaises(ProviderError):
                provider(Recorder(raw)).complete([], timeout=1)

    def test_openrouter_decisions_rejects_non_empty_tools(self) -> None:
        send = Recorder(b"")
        decisions = OpenRouterDecisions(api_key="test-key", send=send)
        req = DecisionsRequest(state={"s": "x"}, questions={"c0": Choice("Q?", {"match": "a", "maybe": "b", "no": "c"})})
        tool = ToolSpec("lookup", "Lookup", {"type": "object"})
        with self.assertRaises(TypeError):
            decisions.complete(req, timeout=1, tools=[tool])
        self.assertEqual(send.requests, [])


class FakeProviderToolTests(unittest.TestCase):
    def test_fake_provider_tool_call_and_call_tools(self) -> None:
        spec = ToolSpec("lookup", "Look up", {"type": "object", "properties": {"id": {"type": "string"}}})
        fake = (
            FakeProvider("m1")
            .tool_call("lookup", {"id": "[id 1]"}, prompt_tokens=11, completion_tokens=4)
            .tool_call("ping", call_id="custom_id", content="calling ping", model="m2")
            .reply('{"done": true}', prompt_tokens=5, completion_tokens=2)
        )
        first = fake.complete([{"role": "user", "content": "hi"}], timeout=5, zdr=True, tools=[spec])
        second = fake.complete([{"role": "user", "content": "next"}], timeout=5, tools=[spec])
        third = fake.complete([{"role": "user", "content": "end"}], timeout=5)

        self.assertIsNone(first.content)
        self.assertEqual(first.tool_calls, (ToolCall(id="call_1", name="lookup", arguments={"id": "[id 1]"}),))
        self.assertEqual(first.tokens, Tokens(11, 4))
        self.assertEqual(second.content, "calling ping")
        self.assertEqual(second.model, "m2")
        self.assertEqual(second.tool_calls, (ToolCall(id="custom_id", name="ping", arguments={}),))
        self.assertEqual(third.content, '{"done": true}')
        self.assertEqual(third.tool_calls, ())

        self.assertEqual(fake.calls[0].tools, (spec,))
        self.assertEqual(fake.calls[1].tools, (spec,))
        self.assertEqual(fake.calls[2].tools, ())

    def test_fake_provider_tool_calls_accepts_sequence_or_varargs(self) -> None:
        c1 = ToolCall("c1", "a", {"x": 1})
        c2 = ToolCall("c2", "b", {"y": 2})
        fake = FakeProvider().tool_calls([c1, c2], prompt_tokens=3).tool_calls(c1, c2, completion_tokens=4)

        first = fake.complete([], timeout=1)
        second = fake.complete([], timeout=1)
        self.assertEqual(first.tool_calls, (c1, c2))
        self.assertEqual(first.tokens, Tokens(3, 0))
        self.assertEqual(second.tool_calls, (c1, c2))
        self.assertEqual(second.tokens, Tokens(0, 4))

    def test_fake_provider_decisions_rejects_tools(self) -> None:
        fake = FakeProvider().decisions(
            {
                "model": "jev",
                "answers": {"c0": {"type": "choice", "choice": "match", "probabilities": {"match": 1, "maybe": 0, "no": 0}}},
            }
        )
        req = DecisionsRequest({}, {"c0": Choice("Q?", {"match": "a", "maybe": "b", "no": "c"})})
        with self.assertRaises(TypeError):
            fake.complete(req, timeout=1, tools=[ToolSpec("t", "d", {})])


class ResultMetadataTests(unittest.TestCase):
    def test_tokens_add(self) -> None:
        self.assertEqual(Tokens(10, 5).add(Tokens(20, 3)), Tokens(30, 8))
        self.assertEqual(Tokens().add(Tokens(7, 2)), Tokens(7, 2))
        self.assertEqual(Tokens().add(Tokens(None, 2)), Tokens(None, 2))
        self.assertIsNone(Tokens().add(Tokens(None, 2)).total)
        self.assertEqual(Tokens(10, 5).add(Tokens(None, None)), Tokens(None, None))

    def test_trace_defaults_and_skill_metadata(self) -> None:
        legacy = Trace("tag", "model", Tokens(1, 2), 42)
        self.assertFalse(legacy.repaired)
        self.assertIsNone(legacy.skill_name)
        self.assertIsNone(legacy.skill_hash)
        self.assertEqual(legacy.turns, 1)
        self.assertEqual(legacy.script_runs, ())

        phase: ScriptPhase = "pre"
        run_meta = ScriptRun(name="normalize", phase=phase, exit_code=0, duration_ms=15)
        skilled = Trace(
            "tag",
            "model",
            Tokens(10, 5),
            100,
            skill_name="my-skill",
            skill_hash="abc123",
            turns=3,
            script_runs=(run_meta,),
        )
        self.assertEqual(skilled.skill_name, "my-skill")
        self.assertEqual(skilled.skill_hash, "abc123")
        self.assertEqual(skilled.turns, 3)
        self.assertEqual(skilled.script_runs, (run_meta,))


if __name__ == "__main__":
    unittest.main()
