"""Typed LLM tasks: masked input in, a validated output model or a typed failure out.

Install with the ``llm`` extra: ``pip install 't6[llm]'``. Output models are
pydantic v2 models. Tests use :mod:`t6.llm.testing`, never a live provider.
"""

from t6.llm.result import Failed, Ok, Reason, Result, Tokens, Trace
from t6.llm.task import DEFAULT_TIMEOUT, MissingSession, Task, UnmaskedInput, run
from t6.llm.transport import Completion, Message, OpenAIChat, Provider, ProviderError, ProviderTimeout

__all__ = [
    "DEFAULT_TIMEOUT",
    "Completion",
    "Failed",
    "Message",
    "MissingSession",
    "Ok",
    "OpenAIChat",
    "Provider",
    "ProviderError",
    "ProviderTimeout",
    "Reason",
    "Result",
    "Task",
    "Tokens",
    "Trace",
    "UnmaskedInput",
    "run",
]
