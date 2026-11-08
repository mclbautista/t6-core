"""Typed LLM tasks: masked input in, a validated output model or a typed failure out.

Install with the ``llm`` extra: ``pip install 't6[llm]'``. Output models are
pydantic v2 models. Tests use :mod:`t6.llm.testing`, never a live provider.
"""

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
from t6.llm.result import Failed, Ok, Reason, Result, Tokens, Trace
from t6.llm.task import DEFAULT_TIMEOUT, MissingSession, Task, UnmaskedInput, run
from t6.llm.transport import (
    Choice,
    Completion,
    DecisionsRequest,
    Message,
    OpenAIChat,
    Provider,
    ProviderError,
    ProviderTimeout,
    Request,
)

__all__ = [
    "DECISIONS_URL",
    "DEFAULT_TIMEOUT",
    "JEV_MODEL",
    "CandidateDecision",
    "CandidateDecisions",
    "CandidateKeys",
    "Choice",
    "Completion",
    "DecisionsRequest",
    "Failed",
    "MalformedReply",
    "Message",
    "MissingSession",
    "Ok",
    "OpenAIChat",
    "OpenRouterDecisions",
    "Probabilities",
    "Provider",
    "ProviderError",
    "ProviderTimeout",
    "Reason",
    "Request",
    "Result",
    "Task",
    "Tokens",
    "Trace",
    "UnmaskedInput",
    "candidate_key",
    "run",
]
