"""OpenAIChat request and response mapping, with a stubbed send. No network, no recorded replies."""

import json
import socket
import unittest
import urllib.error
from email.message import Message as Headers

from t6.llm import OpenAIChat, ProviderError, ProviderTimeout, Tokens


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


if __name__ == "__main__":
    unittest.main()

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
