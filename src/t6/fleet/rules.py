"""Fleet rules: machine names, role groups, the manifest chain and observation staleness.

Pure functions and constants; nothing here does I/O.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime

from t6.domain import Ring

__all__ = [
    "MAX_OBSERVATION_AGE",
    "RINGS",
    "ROLE_GROUPS",
    "SITE_MANIFESTS",
    "ManifestChainError",
    "age_seconds",
    "is_stale",
    "manifest_chain",
    "match_key",
    "role_group",
]

RINGS: tuple[Ring, ...] = tuple(Ring)
"""Software is imported into testing, verified on the pilot machines, then promoted to production."""

ROLE_GROUPS: tuple[str, ...] = (
    "toc",
    "sound",
    "production",
    "online",
    "dit",
    "warroom",
    "coordination-admin",
    "client-facing",
    "interns-temporary",
)
"""The nine role group manifests. A machine manifest includes exactly one."""

SITE_MANIFESTS: tuple[str, ...] = ("all", "site_default")
"""Site level manifests, not machines: they exist so the chain resolves."""

MAX_OBSERVATION_AGE = 3600
"""Seconds after which an observation is stale."""

_ARD_SUFFIX = re.compile(r"-\d{2,4}$")


class ManifestChainError(ValueError):
    """A manifest chain cannot be resolved: a manifest is missing or includes itself."""


def match_key(name: str) -> str:
    """A key to compare machine names: case, spaces and ARD's numeric suffix are not identity.

    ARD appends a collision suffix to the UNIX hostname (`201-Bay-01-Mac-Pro-111`)
    and some names have spaces where the manifest has hyphens (`303B iMac`, `303B-iMac`).
    Use the key for matching only; report the source's own spelling.
    """
    folded = re.sub(r"-+", "-", name.strip().lower().replace(" ", "-").replace("_", "-"))
    return _ARD_SUFFIX.sub("", folded)


def role_group(included_manifests: Sequence[str]) -> str | None:
    """The role group a machine manifest includes. None when it includes none or more than one."""
    groups = [name for name in included_manifests if name in ROLE_GROUPS]
    return groups[0] if len(groups) == 1 else None


def manifest_chain(name: str, included_manifests: Mapping[str, Sequence[str]]) -> tuple[str, ...]:
    """The manifest `name` and every manifest it includes, each once, in include order.

    `included_manifests` maps a manifest name to its `included_manifests` list.
    A machine manifest resolves to itself, its role group, `all` and `site_default`.
    A missing manifest or a manifest that includes itself raises `ManifestChainError`.
    """
    chain: list[str] = []

    def visit(current: str, path: tuple[str, ...]) -> None:
        if current in path:
            raise ManifestChainError(f"manifest {current} includes itself: {' > '.join((*path, current))}")
        if current not in included_manifests:
            raise ManifestChainError(f"manifest {current} does not exist (included by {' > '.join(path) or 'nothing'})")
        if current in chain:
            return
        chain.append(current)
        for child in included_manifests[current]:
            visit(child, (*path, current))

    visit(name, ())
    return tuple(chain)


def age_seconds(observed_at: datetime, now: datetime) -> int:
    """How many seconds old an observation is. Both times must have a timezone."""
    if observed_at.tzinfo is None or now.tzinfo is None:
        raise ValueError("observation times need a timezone")
    return int((now - observed_at).total_seconds())


def is_stale(observed_at: datetime | None, now: datetime, max_age: int = MAX_OBSERVATION_AGE) -> bool:
    """True when there is no observation time, or the observation is older than `max_age` seconds."""
    return observed_at is None or age_seconds(observed_at, now) > max_age
