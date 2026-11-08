"""Provider choice and retention, read from the app's config instead of code.

The app parses its own config file (YAML, TOML or JSON) and passes the ``llm``
section here as a plain mapping::

    llm:
      default: deepseek
      providers:
        deepseek:
          base_url: https://api.deepseek.com
          model: deepseek-chat
          retention: unknown
          api_key_env: DEEPSEEK_API_KEY
        openrouter:
          base_url: https://openrouter.ai/api/v1
          model: some/model
          retention: zdr_enforced
          api_key_env: OPENROUTER_API_KEY
      tasks:
        summarise-v1: openrouter

Every provider must declare ``retention``. API keys never sit in config: each
provider names the environment variable that holds its key.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Mapping
from typing import Any, TypeVar

from t6.llm.policy import RETENTIONS, Retention
from t6.llm.transport import OpenAIChat, Provider, Send

_PROVIDER_KEYS = frozenset({"base_url", "model", "retention", "api_key_env", "json_mode"})
_TOP_KEYS = frozenset({"default", "providers", "tasks"})

K = TypeVar("K")


class ConfigError(ValueError):
    """The ``llm`` config is missing a value, names an unknown provider, or has a value of the wrong type."""


@dataclasses.dataclass(frozen=True)
class ProviderConfig:
    """One provider entry. The key itself is looked up in the environment, never stored here."""

    name: str
    base_url: str
    model: str
    retention: Retention
    api_key_env: str
    json_mode: bool = True


@dataclasses.dataclass(frozen=True)
class LLMConfig:
    """Which provider each task uses, and what each provider declares."""

    providers: Mapping[str, ProviderConfig]
    tasks: Mapping[str, str]
    default: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> LLMConfig:
        """Validate the ``llm`` section of an app's config. Raises :class:`ConfigError`."""
        if not isinstance(data, Mapping):
            raise ConfigError("llm config must be a mapping")
        _no_unknown_keys("llm", data, _TOP_KEYS)
        raw_providers = data.get("providers")
        if not isinstance(raw_providers, Mapping) or not raw_providers:
            raise ConfigError("llm.providers must name at least one provider")
        providers = {str(name): _provider(str(name), entry) for name, entry in raw_providers.items()}

        raw_tasks = data.get("tasks") or {}
        if not isinstance(raw_tasks, Mapping):
            raise ConfigError("llm.tasks must map prompt tags to provider names")
        tasks: dict[str, str] = {}
        for tag, name in raw_tasks.items():
            tasks[str(tag)] = _known(f"llm.tasks.{tag}", name, providers)

        default = data.get("default")
        if default is not None:
            default = _known("llm.default", default, providers)
        return cls(providers, tasks, default)

    def provider_name(self, prompt_tag: str) -> str:
        """The provider configured for ``prompt_tag``, else the default. Raises :class:`ConfigError`."""
        name = self.tasks.get(prompt_tag, self.default)
        if name is None:
            raise ConfigError(f"no provider configured for task {prompt_tag!r} and no llm.default")
        return name

    def provider_for(
        self,
        prompt_tag: str,
        *,
        env: Mapping[str, str] | None = None,
        send: Send | None = None,
    ) -> Provider:
        """Build the provider for ``prompt_tag``, reading its API key from ``env`` (default ``os.environ``).

        Raises :class:`ConfigError` if no provider is configured or its key variable is unset.
        """
        entry = self.providers[self.provider_name(prompt_tag)]
        source = os.environ if env is None else env
        api_key = source.get(entry.api_key_env, "")
        if not api_key:
            raise ConfigError(f"provider {entry.name!r}: environment variable {entry.api_key_env} is not set")
        extra: dict[str, Send] = {} if send is None else {"send": send}
        return OpenAIChat(
            base_url=entry.base_url,
            api_key=api_key,
            model=entry.model,
            retention=entry.retention,
            json_mode=entry.json_mode,
            **extra,
        )


def _provider(name: str, entry: object) -> ProviderConfig:
    where = f"llm.providers.{name}"
    if not isinstance(entry, Mapping):
        raise ConfigError(f"{where} must be a mapping")
    _no_unknown_keys(where, entry, _PROVIDER_KEYS)
    retention = entry.get("retention")
    if retention not in RETENTIONS:
        raise ConfigError(f"{where}.retention must be one of {', '.join(RETENTIONS)}, got {retention!r}")
    json_mode = entry.get("json_mode", True)
    if not isinstance(json_mode, bool):
        raise ConfigError(f"{where}.json_mode must be true or false")
    return ProviderConfig(
        name=name,
        base_url=_text(where, entry, "base_url"),
        model=_text(where, entry, "model"),
        retention=retention,
        api_key_env=_text(where, entry, "api_key_env"),
        json_mode=json_mode,
    )


def _text(where: str, entry: Mapping[str, Any], key: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}.{key} must be a non-empty string")
    return value.strip()


def _known(where: str, name: object, providers: Mapping[str, K]) -> str:
    if not isinstance(name, str) or name not in providers:
        raise ConfigError(f"{where} names unknown provider {name!r}")
    return name


def _no_unknown_keys(where: str, data: Mapping[Any, Any], allowed: frozenset[str]) -> None:
    unknown = sorted(str(key) for key in data if key not in allowed)
    if unknown:
        # An api_key here would be a secret in a config file; name it plainly.
        hint = " (put API keys in the environment and name the variable in api_key_env)" if "api_key" in unknown else ""
        raise ConfigError(f"{where}: unknown keys {', '.join(unknown)}{hint}")


__all__ = ["ConfigError", "LLMConfig", "ProviderConfig"]

