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

NOTIFY_URL_VARIABLE = 'T6_GOOGLE_NOTIFY_URL'
CHANNEL_TOKEN_VARIABLE = 'T6_GOOGLE_CHANNEL_TOKEN'
CHANNEL_TOKEN_MAX = 256  # Google's limit for a channel token


def notify_url(environ: Mapping[str, str]) -> str | None:
    """The public HTTPS address of the edge route Google sends notifications to."""
    return (environ.get(NOTIFY_URL_VARIABLE) or '').strip() or None


def channel_token(environ: Mapping[str, str]) -> str | None:
    """The shared channel token. The edge checks it on every notification (the route's `secret_env` names the same value). Never print it."""
    token = (environ.get(CHANNEL_TOKEN_VARIABLE) or '').strip()
    if len(token) > CHANNEL_TOKEN_MAX:
        raise ValueError(f'{CHANNEL_TOKEN_VARIABLE} must be at most {CHANNEL_TOKEN_MAX} characters')
    return token or None
