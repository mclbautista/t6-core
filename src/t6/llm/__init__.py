"""Typed LLM tasks: masked input in, a validated output model or a typed failure out.

Install with the ``llm`` extra: ``pip install 't6[llm]'``. Output models are
pydantic v2 models. Tests use :mod:`t6.llm.testing`, never a live provider.
"""

from t6.llm.config import ConfigError, LLMConfig, ProviderConfig
from t6.llm.policy import DataPolicy, Masking, Retention
from t6.llm.result import Failed, Ok, Reason, Result, Tokens, Trace
from t6.llm.task import (
    DEFAULT_RATE_LIMIT_WAIT,
    DEFAULT_TIMEOUT,
    MAX_RATE_LIMIT_WAIT,
    MissingSession,
    Task,
    UnmaskedInput,
    run,
)
from t6.llm.transport import (
    Completion,
    Message,
    OpenAIChat,
    Provider,
    ProviderError,
    ProviderTimeout,
    RateLimited,
)

__all__ = [
    "DEFAULT_RATE_LIMIT_WAIT",
    "DEFAULT_TIMEOUT",
    "MAX_RATE_LIMIT_WAIT",
    "Completion",
    "ConfigError",
    "DataPolicy",
    "Failed",
    "LLMConfig",
    "Masking",
    "Message",
    "MissingSession",
    "Ok",
    "OpenAIChat",
    "Provider",
    "ProviderError",
    "ProviderConfig",
    "ProviderTimeout",
    "RateLimited",
    "Reason",
    "Result",
    "Retention",
    "Task",
    "Tokens",
    "Trace",
    "UnmaskedInput",
    "run",
]
