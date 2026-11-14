"""The fleet: Machines in Bays, their facts and the fleet rules. Install with `pip install 't6[notion]'`.

- `t6.fleet.rules`: rings, role groups, the manifest chain, machine name
  matching and observation staleness. No I/O.
- `Fleet`: read Bays as `t6.domain.Machine` and write the fleet facts (dry run
  by default; never creates a row).

munki-repo keeps catalogs and manifests and calls this through the `t6` CLI.
"""

from t6.fleet.bays import FactChange, Fleet, MachineFacts, SyncResult
from t6.fleet.rules import (
    MAX_OBSERVATION_AGE,
    RINGS,
    ROLE_GROUPS,
    SITE_MANIFESTS,
    ManifestChainError,
    age_seconds,
    is_stale,
    manifest_chain,
    match_key,
    role_group,
)

__all__ = [
    "MAX_OBSERVATION_AGE",
    "RINGS",
    "ROLE_GROUPS",
    "SITE_MANIFESTS",
    "FactChange",
    "Fleet",
    "MachineFacts",
    "ManifestChainError",
    "SyncResult",
    "age_seconds",
    "is_stale",
    "manifest_chain",
    "match_key",
    "role_group",
]
