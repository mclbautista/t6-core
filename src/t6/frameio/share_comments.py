"""A guest's name from the share viewer's GraphQL API (#601).

The public share page carries no comment data. Its viewer asks `api.frame.io/graphql` for the comments
(`GetShareCommentsByAsset`) and finds the commenter at `owner.name`. The call needs the share's own token, which
Frame.io gives after the passphrase is accepted; the Frame.io account sign-in is refused (403). Reading it is best
effort: every failure answers None and the caller keeps its guest fallback.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import urllib.error
import urllib.request
from typing import Any

GRAPHQL_URL = "https://api.frame.io/graphql"
USER_AGENT = "t6-frameio-worker"
TIMEOUT_SECONDS = 5.0
LIMIT_BYTES = 2_000_000
NAME_LIMIT = 80
QUERY = (
    "query GetShareCommentsByAsset($shareId: ID!, $assetId: ID!) {"
    " share(shareId: $shareId) { id asset(assetId: $assetId) { id comments { result {"
    " id owner { __typename ... on IdentifiedUser { name } } } } } } }"
)

GraphqlPost = Callable[[Mapping[str, str], bytes], Mapping[str, Any] | None]
"""`(headers, body)` to the answer's JSON object, or None when the call fails."""


def post_graphql(headers: Mapping[str, str], body: bytes) -> Mapping[str, Any] | None:
    request = urllib.request.Request(GRAPHQL_URL, data=body, headers={**headers, "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - fixed https URL
            raw = response.read(LIMIT_BYTES + 1)
        answer = json.loads(raw) if len(raw) <= LIMIT_BYTES else None
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return answer if isinstance(answer, dict) else None


def share_guest_name(post: GraphqlPost, token: str, share_id: str, asset_id: str, comment_id: str) -> str | None:
    """The name Frame.io shows for comment `comment_id` of the asset in the share, or None. Only that comment counts."""
    body = json.dumps({"operationName": "GetShareCommentsByAsset", "query": QUERY,
                       "variables": {"shareId": share_id, "assetId": asset_id}}).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json", "x-frameio-share-authentication": token}
    try:
        answer = post(headers, body)
        results = answer["data"]["share"]["asset"]["comments"]["result"] if answer else None
        for comment in results or ():
            if comment.get("id") == comment_id and isinstance(owner := comment.get("owner"), Mapping):
                name = owner.get("name")
                if isinstance(name, str):
                    clean = "".join(ch for ch in name if ch.isprintable()).strip()[:NAME_LIMIT]
                    return clean or None
    except (KeyError, TypeError, AttributeError):
        return None
    return None
