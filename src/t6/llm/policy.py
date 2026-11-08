"""Data policy: which providers a task may send to, and how its input must be masked.

Each provider declares its retention. Each task declares the minimum retention
it accepts and the masking its input must carry. :func:`t6.llm.run` checks both
before anything is sent: a provider below the task's minimum returns
``Failed("policy")``, and input masked the wrong way raises
:class:`~t6.llm.UnmaskedInput`.
"""

from __future__ import annotations

import dataclasses
from typing import Literal, get_args

Retention = Literal["unknown", "zdr_enforced"]
"""What a provider promises about prompt retention.

``zdr_enforced``: zero data retention is enforced for every request (for
example OpenRouter with ``provider.zdr``). ``unknown``: no such promise.
"""

Masking = Literal["withhold", "pseudonymize"]
"""How a task's input text must be masked.

``withhold``: every text value is a :class:`~t6.privacy.Withheld` (credentials
removed, names kept). ``pseudonymize``: every text value is a
:class:`~t6.privacy.Masked` (names swapped for placeholders, restored after).
"""

RETENTIONS: tuple[Retention, ...] = get_args(Retention)
MASKINGS: tuple[Masking, ...] = get_args(Masking)

# Weakest first: a provider meets a minimum at or below its own rank.
_RANK: dict[Retention, int] = {"unknown": 0, "zdr_enforced": 1}


@dataclasses.dataclass(frozen=True)
class DataPolicy:
    """What a task requires. The default is the strictest policy."""

    min_retention: Retention = "zdr_enforced"
    masking: Masking = "pseudonymize"

    def __post_init__(self) -> None:
        if self.min_retention not in RETENTIONS:
            raise ValueError(f"min_retention must be one of {', '.join(RETENTIONS)}, got {self.min_retention!r}")
        if self.masking not in MASKINGS:
            raise ValueError(f"masking must be one of {', '.join(MASKINGS)}, got {self.masking!r}")

    def allows(self, retention: Retention) -> bool:
        """True if a provider declaring ``retention`` meets this policy's minimum."""
        return retention in _RANK and _RANK[retention] >= _RANK[self.min_retention]
