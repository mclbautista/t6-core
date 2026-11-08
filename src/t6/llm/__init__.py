"""Typed LLM tasks: masked input in, a validated output model or a typed failure out.

Install with the ``llm`` extra: ``pip install 't6[llm]'``. Output models are
pydantic v2 models. Tests use :mod:`t6.llm.testing`, never a live provider.
"""

from t6.llm.config import ConfigError, LLMConfig, ProviderConfig
from t6.llm.decisions import (
    DECISIONS_URL,
    JEV_MODEL,
    CandidateDecision,
    CandidateDecisions,
    CandidateKeys,
    MalformedReply,
    OpenRouterDecisions,
    Probabilities,
    candidate_key,
)
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
    Choice,
    Completion,
    DecisionsRequest,
    Message,
    OpenAIChat,
    Provider,
    ProviderError,
    ProviderTimeout,
    RateLimited,
    Request,
)

__all__ = [
    "DECISIONS_URL",
    "DEFAULT_RATE_LIMIT_WAIT",
    "DEFAULT_TIMEOUT",
    "JEV_MODEL",
    "MAX_RATE_LIMIT_WAIT",
    "CandidateDecision",
    "CandidateDecisions",
    "CandidateKeys",
    "Choice",
    "Completion",
    "ConfigError",
    "DataPolicy",
    "DecisionsRequest",
    "Failed",
    "LLMConfig",
    "MalformedReply",
    "Masking",
    "Message",
    "MissingSession",
    "Ok",
    "OpenAIChat",
    "OpenRouterDecisions",
    "Probabilities",
    "Provider",
    "ProviderError",
    "ProviderConfig",
    "ProviderTimeout",
    "RateLimited",
    "Reason",
    "Request",
    "Result",
    "Retention",
    "Task",
    "Tokens",
    "Trace",
    "UnmaskedInput",
    "candidate_key",
    "run",
]
