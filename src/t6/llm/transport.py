"""Providers: the transports a task's messages travel through.

A provider sends chat messages and returns a :class:`Completion`. It raises
:class:`ProviderTimeout` or :class:`ProviderError` for transport failures, and
the adapter turns those into ``failed`` results. :class:`OpenAIChat` speaks the
OpenAI-style chat completions API that DeepSeek and OpenRouter both serve.
"""

from __future__ import annotations

import dataclasses
import json
import socket
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from typing import Any, Literal, Protocol, TypedDict

from t6.llm.result import Tokens


class Message(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


@dataclasses.dataclass(frozen=True)
class Completion:
    """A provider's reply. ``content`` is ``None`` when the reply had no text."""

    content: str | None
    model: str
    tokens: Tokens = Tokens()


class ProviderTimeout(Exception):
    """The provider did not answer in time."""


class ProviderError(Exception):
    """The provider answered with an error, or with a reply the transport cannot read."""


class Provider(Protocol):
    @property
    def model(self) -> str: ...

    def complete(self, messages: Sequence[Message], *, timeout: float) -> Completion: ...


# (url, headers, body, timeout) -> response body. Raises urllib or socket errors.
Send = Callable[[str, dict[str, str], bytes, float], bytes]


def _urlopen(url: str, headers: dict[str, str], body: bytes, timeout: float) -> bytes:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data: bytes = response.read()
        return data


class OpenAIChat:
    """An OpenAI-style ``/chat/completions`` endpoint, such as DeepSeek or OpenRouter.

    ``base_url`` is the API root, for example ``https://api.deepseek.com`` or
    ``https://openrouter.ai/api/v1``. The API key comes from the app's config.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        json_mode: bool = True,
        send: Send = _urlopen,
    ) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._model = model
        self._json_mode = json_mode
        self._send = send

    @property
    def model(self) -> str:
        return self._model

    def complete(self, messages: Sequence[Message], *, timeout: float) -> Completion:
        payload: dict[str, Any] = {"model": self._model, "messages": list(messages)}
        if self._json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        try:
            raw = self._send(self._url, headers, json.dumps(payload).encode(), timeout)
        except urllib.error.HTTPError as error:
            # The body can echo the request, so only the status is kept.
            raise ProviderError(f"HTTP {error.code}") from None
        except (TimeoutError, socket.timeout) as error:
            raise ProviderTimeout(str(error) or "timed out") from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise ProviderTimeout("timed out") from None
            raise ProviderError(f"connection failed: {type(error.reason).__name__}") from None
        except OSError as error:
            raise ProviderError(f"connection failed: {type(error).__name__}") from None
        return _completion(raw, self._model)


def _completion(raw: bytes, requested_model: str) -> Completion:
    try:
        body = json.loads(raw)
        if "error" in body:
            raise ProviderError("provider returned an error body")
        message = body["choices"][0]["message"]
        content = message.get("content")
        usage = body.get("usage") or {}
        tokens = Tokens(int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0))
        model = body.get("model") or requested_model
    except ProviderError:
        raise
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise ProviderError("unreadable provider response") from None
    if content is not None and not isinstance(content, str):
        raise ProviderError("unreadable provider response")
    return Completion(content=content, model=str(model), tokens=tokens)
