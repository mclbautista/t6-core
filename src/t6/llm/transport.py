"""Providers: the transports a task's messages travel through.

A provider sends a request and returns a :class:`Completion`. A request is
either chat messages or a :class:`DecisionsRequest`; each transport accepts
one kind and raises ``TypeError`` for the other. It raises
:class:`ProviderTimeout` or :class:`ProviderError` for transport failures, and
the adapter turns those into ``failed`` results. A 429 raises
:class:`RateLimited`, which the adapter waits on once. Every provider declares
its :data:`~t6.llm.policy.Retention`. :class:`OpenAIChat` speaks the
OpenAI-style chat completions API that DeepSeek and OpenRouter both serve;
:class:`~t6.llm.decisions.OpenRouterDecisions` speaks the Decisions API (Jev).
"""

from __future__ import annotations

import base64
import copy
import dataclasses
import email.utils
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from enum import Enum
from typing import Any, Literal, NotRequired, Protocol, TypedDict

from t6.llm.policy import RETENTIONS, Retention
from t6.llm.result import Tokens


class ImageType(str, Enum):
    png = "image/png"
    jpeg = "image/jpeg"
    webp = "image/webp"


@dataclasses.dataclass(frozen=True)
class Image:
    """In-memory photo input. Neither bytes nor their encoding belong in traces."""

    data: bytes = dataclasses.field(repr=False)
    media_type: ImageType = ImageType.png


class Message(TypedDict):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    image: NotRequired[Image]
    tool_call_id: NotRequired[str]
    tool_calls: NotRequired[list[dict[str, Any]]]


@dataclasses.dataclass(frozen=True)
class ToolSpec:
    """One callable tool declaration offered to a chat provider."""

    name: str
    description: str
    parameters: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.parameters, dict):
            object.__setattr__(self, "parameters", dict(self.parameters))


@dataclasses.dataclass(frozen=True)
class ToolCall:
    """One tool invocation requested by a chat provider."""

    id: str
    name: str
    arguments: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.arguments, dict):
            object.__setattr__(self, "arguments", dict(self.arguments))


@dataclasses.dataclass(frozen=True)
class Choice:
    """One typed Choice question for the Decisions API. ``criteria`` describes ``match``, ``maybe`` and ``no``."""

    instructions: str
    criteria: Mapping[str, str]


@dataclasses.dataclass(frozen=True)
class DecisionsRequest:
    """What a Decisions task renders: masked ``state`` and one question per candidate key (``c0``, ``c1``, ...)."""

    state: Mapping[str, str]
    questions: Mapping[str, Choice]


Request = Sequence[Message] | DecisionsRequest
"""What a task's ``render`` returns and a provider sends."""


@dataclasses.dataclass(frozen=True)
class Completion:
    """A provider's reply. ``content`` is ``None`` when the reply had no text."""

    content: str | None
    model: str
    tokens: Tokens = Tokens()
    tool_calls: tuple[ToolCall, ...] = ()
    cost: float | None = None  # USD when reported by the provider


class ProviderTimeout(Exception):
    """The provider did not answer in time."""


class ProviderError(Exception):
    """The provider answered with an error, or with a reply the transport cannot read."""


class RateLimited(ProviderError):
    """The provider answered 429. ``retry_after`` is in seconds, ``None`` when it sent no usable header."""

    def __init__(self, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__("HTTP 429" if retry_after is None else f"HTTP 429, retry after {retry_after:g}s")


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    """Seconds to wait from a ``Retry-After`` header (delay seconds or an HTTP date), else ``None``."""
    if value is None or not value.strip():
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            return None
        seconds = when.timestamp() - (time.time() if now is None else now)
    if seconds != seconds:  # NaN
        return None
    return max(0.0, seconds)


class Provider(Protocol):
    @property
    def model(self) -> str: ...

    @property
    def retention(self) -> Retention: ...

    def complete(
        self,
        request: Request,
        *,
        timeout: float,
        zdr: bool = False,
        tools: Sequence[ToolSpec] = (),
    ) -> Completion: ...


# (url, headers, body, timeout) -> response body. Raises urllib or socket errors.
Send = Callable[[str, dict[str, str], bytes, float], bytes]


def _urlopen(url: str, headers: dict[str, str], body: bytes, timeout: float) -> bytes:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data: bytes = response.read()
        return data


def _post(send: Send, url: str, headers: dict[str, str], body: bytes, timeout: float) -> bytes:
    """Send one request; map transport failures to ``RateLimited``, ``ProviderTimeout`` or ``ProviderError``."""
    try:
        return send(url, headers, body, timeout)
    except urllib.error.HTTPError as error:
        if error.code == 429:
            headers_in = error.headers
            raise RateLimited(parse_retry_after(headers_in.get("Retry-After") if headers_in else None)) from None
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


class OpenAIChat:
    """An OpenAI-style ``/chat/completions`` endpoint, such as DeepSeek or OpenRouter.

    ``base_url`` is the API root, for example ``https://api.deepseek.com`` or
    ``https://openrouter.ai/api/v1``. The API key comes from the app's config.
    ``retention`` is what this endpoint promises; it defaults to ``unknown``.
    For a ``zdr_enforced`` task on OpenRouter the request also carries
    ``provider.zdr: true``, which asks OpenRouter for zero data retention.
    Build providers from config with :class:`t6.llm.LLMConfig`.
    Image input requires an explicitly declared ``supports_images=True``
    for the configured endpoint and model; the default refuses before sending.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        retention: Retention = "unknown",
        json_mode: bool = True,
        supports_images: bool = False,
        send: Send = _urlopen,
    ) -> None:
        if retention not in RETENTIONS:
            raise ValueError(f"retention must be one of {', '.join(RETENTIONS)}, got {retention!r}")
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._model = model
        self._retention: Retention = retention
        self._json_mode = json_mode
        self._supports_images = supports_images
        self._temperature: float | None = None
        self._send = send
        self._openrouter = urllib.parse.urlparse(base_url).hostname == "openrouter.ai"

    @property
    def model(self) -> str:
        return self._model

    @property
    def retention(self) -> Retention:
        return self._retention

    def with_temperature(self, temperature: float) -> OpenAIChat:
        """Return a provider copy with an explicit sampling temperature."""
        provider = copy.copy(self)
        provider._temperature = temperature
        return provider

    def complete(
        self,
        request: Request,
        *,
        timeout: float,
        zdr: bool = False,
        tools: Sequence[ToolSpec] = (),
    ) -> Completion:
        if isinstance(request, DecisionsRequest):
            raise TypeError("OpenAIChat sends chat messages; use OpenRouterDecisions for a DecisionsRequest")
        messages: list[dict[str, Any]] = []
        for message in request:
            rendered: dict[str, Any] = dict(message)
            image = rendered.pop("image", None)
            if image is not None:
                if not self._supports_images:
                    raise ProviderError("provider does not support image input")
                encoded = base64.b64encode(image.data).decode("ascii")
                rendered["content"] = [
                    {"type": "text", "text": message["content"]},
                    {"type": "image_url", "image_url": {
                        "url": f"data:{image.media_type.value};base64,{encoded}",
                    }},
                ]
            messages.append(rendered)
        payload: dict[str, Any] = {"model": self._model, "messages": messages}
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": dict(tool.parameters),
                    },
                }
                for tool in tools
            ]
        if self._temperature is not None:
            payload["temperature"] = self._temperature
        if self._json_mode:
            payload["response_format"] = {"type": "json_object"}
        if zdr and self._openrouter:
            payload["provider"] = {"zdr": True}
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        raw = _post(self._send, self._url, headers, json.dumps(payload).encode(), timeout)
        return _completion(raw, self._model)


def _parse_tool_calls(raw_calls: object) -> tuple[ToolCall, ...]:
    if raw_calls is None:
        return ()
    if not isinstance(raw_calls, list):
        raise ProviderError("unreadable provider response")
    parsed: list[ToolCall] = []
    for entry in raw_calls:
        if not isinstance(entry, dict):
            raise ProviderError("unreadable provider response")
        call_type = entry.get("type")
        if call_type is not None and call_type != "function":
            raise ProviderError("unreadable provider response")
        call_id = entry.get("id")
        function = entry.get("function")
        if not isinstance(call_id, str) or not call_id or not isinstance(function, dict):
            raise ProviderError("unreadable provider response")
        name = function.get("name")
        raw_args = function.get("arguments", "{}")
        if not isinstance(name, str) or not name:
            raise ProviderError("unreadable provider response")
        if isinstance(raw_args, str):
            args_obj: object = json.loads(raw_args) if raw_args.strip() else {}
        elif isinstance(raw_args, dict):
            args_obj = raw_args
        else:
            raise ProviderError("unreadable provider response")
        if not isinstance(args_obj, dict):
            raise ProviderError("unreadable provider response")
        parsed.append(ToolCall(id=call_id, name=name, arguments=args_obj))
    return tuple(parsed)


def _completion(raw: bytes, requested_model: str) -> Completion:
    try:
        body = json.loads(raw)
        if "error" in body:
            raise ProviderError("provider returned an error body")
        message = body["choices"][0]["message"]
        content = message.get("content")
        tool_calls = _parse_tool_calls(message.get("tool_calls"))
        usage = body.get("usage") or {}
        tokens = Tokens(*(int(value) if value is not None else None
                          for value in (usage.get("prompt_tokens"), usage.get("completion_tokens"))))
        model = body.get("model") or requested_model
    except ProviderError:
        raise
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise ProviderError("unreadable provider response") from None
    if content is not None and not isinstance(content, str):
        raise ProviderError("unreadable provider response")
    return Completion(content=content, model=str(model), tokens=tokens, tool_calls=tool_calls,
                      cost=float(usage['cost']) if isinstance(usage.get('cost'), (int, float)) else None)
