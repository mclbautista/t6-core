"""Client for the server's `POST /review/timers`, used by the Frame.io worker.

The Frame.io worker reads a comment, then tells the server to start or restart
the quiet timer for the comment's (file, author). The server holds the timers.
"""

from __future__ import annotations

import json
from typing import Any

from t6.frameio.model import ResolvedComment
from t6.worker.client import QueueRejected, Transport


class ReviewTimerClient:
    def __init__(self, transport: Transport, secret: str) -> None:
        self._transport = transport
        self._headers = {"Authorization": f"Bearer {secret}", "Content-Type": "application/json"}

    def arm(self, comment: ResolvedComment) -> None:
        """Count `comment` toward its (file, author) timer. Counting it twice counts it once."""
        body: dict[str, Any] = {
            "comment_id": comment.id,
            "file_id": comment.file_id,
            "frameio_project_id": comment.frameio_project_id,
            "user_id": comment.author_id,
            "user_name": comment.author_name,
            "created_at": comment.created_at.isoformat(),
        }
        status, answer = self._transport.send("POST", "/review/timers", self._headers, json.dumps(body).encode())
        if status != 200:
            raise QueueRejected(status, str(answer.get("error", answer)))
