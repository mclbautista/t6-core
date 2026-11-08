"""Entity lookup: turn a loose mention into the right T6 entity.

`t6.lookup` is named so that `t6.resolve` stays free for DaVinci Resolve.

A mention is a Telegram handle, a name in Job Order text, a Telegram Key, a
Project Code, a hostname or a serial number. Each entity type has one pipeline.
Strategies run cheapest first and stop at the first confident answer:

1. exact ID (Telegram Key, Project Code, Telegram user ID, serial number)
2. normalized match (handle, alias, code, honorifics stripped)
3. fuzzy match
4. LLM judgment (optional: a plain callable, skipped when none is configured)
5. human review

Every call returns a `Resolution`: the entity (or none), the method that
decided, the score, the ranked candidates and `needs_review`. Nothing below
the policy's thresholds is picked silently.

The resolver does no I/O. It works over an `Index` the caller supplies, and a
confirmed alias comes back as a proposed `AliasWrite`, never a write. The base
package has no third-party dependencies, so the default fuzzy scorer is built
on the standard library; pass `Policy(scorer=...)` to use another one (for
example rapidfuzz's WRatio divided by 100).
"""

from t6.lookup.core import AliasWrite, Index, Judge, Lookup, Policy, similarity
from t6.lookup.normalize import DEFAULT_HONORIFICS, Normalizers
from t6.lookup.result import Candidate, Method, Resolution

__all__ = [
    "DEFAULT_HONORIFICS",
    "AliasWrite",
    "Candidate",
    "Index",
    "Judge",
    "Lookup",
    "Method",
    "Normalizers",
    "Policy",
    "Resolution",
    "similarity",
]
