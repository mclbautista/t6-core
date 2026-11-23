"""Guest reviewers' names from Frame.io's private GraphQL API (#545).

A comment a guest left through a share link has no `owner` in the public v4 API. The share page loads its comments,
with each guest's name, from `POST https://api.frame.io/graphql` (operation `GetShareCommentsByAsset`) at
`data.share.asset.comments.result[].owner.name`. On a passphrase share that call needs the header
`x-frameio-share-authentication`.

There is no unlock call (#602): Frame.io's web app builds that header itself as base64 of "<share id>:<passphrase>" on
every call, and the server checks it there. This API is undocumented, so every failure (refused call, a changed
answer) answers no names and the caller keeps its guest fallback.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
import json
from typing import Any
import urllib.error
import urllib.request
import uuid

from t6.frameio.tokens import USER_AGENT

GRAPHQL_URL = "https://api.frame.io/graphql"
TIMEOUT_SECONDS = 5.0
NAME_LIMIT = 80
CLIENT_HEADERS = {  # Frame.io answers PERMISSION_DENIED without the web app's client headers (#602)
    "apollographql-client-name": "web-app",
    "apollographql-client-version": "@frameio/next-web-app@788.1",
}

COMMENTS_QUERY = (
    "query GetShareCommentsByAsset($shareId: ID!, $assetId: ID!) {"
    " share(shareId: $shareId) { asset(assetId: $assetId) { comments {"
    " result { id owner { ... on IdentifiedUser { name } } } } } } }"
)

Post = Callable[[Mapping[str, Any], Mapping[str, str]], Any]
"""POST one GraphQL request (body, extra headers) to Frame.io; the decoded JSON answer. Raises on any failure."""
ShareNames = Callable[[str, str, str | None], dict[str, str]]
"""(share id, file id, passphrase or None) -> {comment id: guest name}; empty on any failure."""


def post_graphql(body: Mapping[str, Any], headers: Mapping[str, str]) -> Any:
    request = urllib.request.Request(
        GRAPHQL_URL,
        data=json.dumps(body).encode(),
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - fixed https URL
        return json.loads(response.read())


def _clean(name: Any) -> str | None:
    if not isinstance(name, str):
        return None
    text = "".join(ch for ch in name if ch.isprintable()).strip()
    return text[:NAME_LIMIT] or None


def _dig(value: Any, *keys: str) -> Any:
    for key in keys:
        value = value.get(key) if isinstance(value, Mapping) else None
    return value


def share_authentication(share_id: str, passphrase: str) -> str:
    """The `x-frameio-share-authentication` value for a passphrase share, built as Frame.io's web app builds it (#602)."""
    return base64.b64encode(f"{share_id}:{passphrase}".encode()).decode()


def share_guest_names(share_id: str, file_id: str, passphrase: str | None, post: Post = post_graphql) -> dict[str, str]:
    """{comment id: name} for the file's comments as the share shows them. Empty when anything fails."""
    try:
        headers = {**CLIENT_HEADERS, "x-gql-op": "GetShareCommentsByAsset", "x-frameio-session-id": str(uuid.uuid4())}
        if passphrase:
            headers["x-frameio-share-authentication"] = share_authentication(share_id, passphrase)
        answer = post({"query": COMMENTS_QUERY, "variables": {"shareId": share_id, "assetId": file_id}}, headers)
    except (urllib.error.URLError, OSError, ValueError):
        return {}
    comments = _dig(answer, "data", "share", "asset", "comments", "result")
    names: dict[str, str] = {}
    for comment in comments if isinstance(comments, list) else ():
        comment_id, name = _dig(comment, "id"), _clean(_dig(comment, "owner", "name"))
        if isinstance(comment_id, str) and name is not None:
            names[comment_id] = name
    return names
