"""The Frame.io worker's read API, for apps that need Frame.io data and hold no token.

Read-only HTTP on the LAN and tailnet (serve it with `t6.server.http.make_server`).
Every route needs `Authorization: Bearer <secret>`. No route returns the
token, and only GET is accepted.

| Route | Answers |
| --- | --- |
| `GET /frameio/status` | `{"value": {"signed_in": true}}` |
| `GET /frameio/projects` | Frame.io projects |
| `GET /frameio/projects/<id>/share-links` | the project's share links |
| `GET /frameio/files/<id>/comments` | the file's comments |
| `GET /frameio/files/<id>/approvals` | the file's approvals |
| `GET /frameio/briefing?since=<ISO 8601 time with zone>` | the briefing feed |

Answers are `{"value": ...}`, the same JSON as `t6.frameio.ask`, or
`{"error": ...}` with 401 (bad secret), 404, 405, 400 (bad request), 503
(`sign_in_required`) or 502 (Frame.io failed). Use `t6.frameio.FrameioReader`
to call it.
"""

from __future__ import annotations

from datetime import datetime
import hmac
import urllib.parse

from t6.domain.events import JsonValue
from t6.frameio.requests import Approvals, BriefingFeed, Comments, FrameioProjects, FrameioRequest, ShareLinks
from t6.frameio.worker import FrameioWorker, SignInRequired
from t6.server.app import Request, Response

SIGN_IN_REQUIRED = "sign_in_required"


class ReadApi:
    def __init__(self, worker: FrameioWorker, secret: str) -> None:
        if not secret:
            raise ValueError("the read API needs a secret")
        self._worker = worker
        self._secret = secret

    def handle(self, request: Request) -> Response:
        if not hmac.compare_digest(request.header("authorization"), f"Bearer {self._secret}"):
            return Response(401, {"error": "unauthorized"})
        parts = urllib.parse.urlsplit(request.path)
        segments = [urllib.parse.unquote(part) for part in parts.path.strip("/").split("/")]
        if segments[:1] != ["frameio"]:
            return Response(404, {"error": "not found"})
        if request.method != "GET":
            return Response(405, {"error": "read only"})
        route = segments[1:]
        try:
            if route == ["status"]:
                return Response(200, {"value": {"signed_in": self._worker.signed_in}})
            query = self._query(route, urllib.parse.parse_qs(parts.query))
        except ValueError as error:
            return Response(400, {"error": str(error)})
        if query is None:
            return Response(404, {"error": "not found"})
        return self._read(query)

    @staticmethod
    def _query(route: list[str], params: dict[str, list[str]]) -> FrameioRequest | None:
        if route == ["projects"]:
            return FrameioProjects()
        if len(route) == 3 and route[0] == "projects" and route[2] == "share-links":
            return ShareLinks(frameio_project_id=route[1])
        if len(route) == 3 and route[0] == "files" and route[2] == "comments":
            return Comments(file_id=route[1])
        if len(route) == 3 and route[0] == "files" and route[2] == "approvals":
            return Approvals(file_id=route[1])
        if route == ["briefing"]:
            since = params.get("since", [""])[0]
            try:
                return BriefingFeed(since=datetime.fromisoformat(since))
            except ValueError:
                raise ValueError("since must be an ISO 8601 time with a time zone") from None
        return None

    def _read(self, query: FrameioRequest) -> Response:
        try:
            value: JsonValue = self._worker.read(query)
        except SignInRequired:
            return Response(503, {"error": SIGN_IN_REQUIRED})
        except Exception as error:  # noqa: BLE001 - the caller gets a failure, never a traceback
            return Response(502, {"error": f"{type(error).__name__}: {error}"})
        return Response(200, {"value": value})
