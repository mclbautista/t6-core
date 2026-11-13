"""Who a Project's coordinators are, the standing cc, and the operator IDs.

- **Coordinators** of a Project are the Contacts in its `T6 Post Coordinator`
  relation (`Project.post_coordinator_ids`).
- The **standing cc** is one Contact, set by `T6_STANDING_CC` (a Contact page
  ID). It is added to every Job Order's cc. It is tagged in a message only when
  a finding needs the person's own action (`STANDING_CC_FINDINGS`).
- **Operator IDs** are the Telegram user IDs allowed to act as operators, set
  by `T6_OPERATOR_IDS` (space or comma separated). They live only here.

Pure functions: nothing here does I/O except reading the environment you pass.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

from t6.domain import Contact, Project

__all__ = [
    "OPERATOR_IDS_VARIABLE",
    "STANDING_CC_FINDINGS",
    "STANDING_CC_VARIABLE",
    "coordinators",
    "operator_ids",
    "standing_cc",
    "tags_standing_cc",
    "with_standing_cc",
]

STANDING_CC_VARIABLE = "T6_STANDING_CC"
OPERATOR_IDS_VARIABLE = "T6_OPERATOR_IDS"

STANDING_CC_FINDINGS = frozenset(
    {"multiple_jo", "unresolved_person", "ambiguous_person", "unresolved_project", "unknown_department"}
)
"""The only findings the standing cc has to act on, so the only ones that tag them."""


def coordinators(project: Project, contacts: Mapping[str, Contact]) -> tuple[Contact, ...]:
    """The Project's coordinators, in relation order, each with their Telegram ID and handle.

    `contacts` maps Contact page IDs to Contacts. A coordinator missing from it
    is left out. Read the Contacts, with `t6.notion.reads`, before calling.
    """
    return tuple(contacts[page_id] for page_id in project.post_coordinator_ids if page_id in contacts)


def standing_cc(environ: Mapping[str, str] | None = None) -> str | None:
    """The standing cc Contact page ID, or None when `T6_STANDING_CC` is unset or blank."""
    value = (environ if environ is not None else os.environ).get(STANDING_CC_VARIABLE, "").strip()
    return value or None


def with_standing_cc(cc_ids: Iterable[str], environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """`cc_ids` with the standing cc added last, once. Unchanged when none is set."""
    ids = tuple(dict.fromkeys(cc_ids))
    standing = standing_cc(environ)
    return ids if standing is None or standing in ids else (*ids, standing)


def tags_standing_cc(finding_codes: Iterable[str]) -> bool:
    """Whether any finding is one the standing cc has to act on."""
    return any(code in STANDING_CC_FINDINGS for code in finding_codes)


def operator_ids(environ: Mapping[str, str] | None = None) -> tuple[int, ...]:
    """The operator Telegram user IDs from `T6_OPERATOR_IDS`. Empty when unset.

    Raises ValueError for a part that is not a whole number.
    """
    text = (environ if environ is not None else os.environ).get(OPERATOR_IDS_VARIABLE, "")
    return tuple(int(part) for part in text.replace(",", " ").split())
