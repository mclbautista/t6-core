"""Google settings. Only the API and the main worker read them (ADR 0003)."""
from __future__ import annotations

import json
from collections.abc import Mapping

CREDENTIAL_VARIABLE = 'GOOGLE_APPLICATION_CREDENTIALS'
CALENDARS_VARIABLE = 'T6_GOOGLE_BAY_CALENDARS'


def bay_calendars(environ: Mapping[str, str]) -> dict[str, str]:
    """Bay id to calendar id, from a JSON object in the environment."""
    raw = environ.get(CALENDARS_VARIABLE, '')
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ValueError(f'{CALENDARS_VARIABLE} must be a JSON object of text values')
    return value


def credential_path(environ: Mapping[str, str]) -> str | None:
    """The service account key file named by the credential variable, if set."""
    return (environ.get(CREDENTIAL_VARIABLE) or '').strip() or None
