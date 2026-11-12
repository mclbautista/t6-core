"""An in-memory fake Frame.io V4 for tests.

`FakeFrameio` answers the routes `FrameioApi` uses, with paging, so tests run
the real client and reads with no network:

    fake = FakeFrameio()
    fake.add_project("fp1", "Example Show")
    fake.add_share("fp1", "s1", "Cut 1", last_viewed_at="2026-11-02T10:00:00Z")
    reads = FrameioReads(FrameioApi(StaticTokens(), fake.fetch))

`requests` records every path asked for. `refuse_next` answers the next
requests with a status (401, 429, 500). Use made-up names only.
"""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any
import urllib.parse

JSON = dict[str, Any]

ACCOUNT = "acc-1"


class StaticTokens:
    """A token store that is always signed in. `refreshes` counts the refresh calls."""

    def __init__(self) -> None:
        self.refreshes = 0

    def access_token(self) -> str:
        return f"access-{self.refreshes}"

    def refresh(self) -> None:
        self.refreshes += 1


class FakeFrameio:
    def __init__(self, page_size: int = 2) -> None:
        self.accounts: list[JSON] = [{"id": ACCOUNT, "display_name": "Example Post"}]
        self.projects: dict[str, JSON] = {}
        self.shares: dict[str, list[JSON]] = {}
        self.activities: dict[str, list[JSON]] = {}
        self.files: dict[str, JSON] = {}
        self.comments: dict[str, list[JSON]] = {}
        self.stacks: dict[str, list[str]] = {}
        self.metadata: dict[str, list[JSON]] = {}
        self.requests: list[str] = []
        self.tokens_seen: list[str] = []
        self._refusals: list[tuple[int, Mapping[str, str]]] = []
        self._page_size = page_size

    # Seeding

    def add_project(self, project_id: str, name: str) -> None:
        self.projects[project_id] = {
            "id": project_id,
            "name": name,
            "workspace_id": "ws-1",
            "root_folder_id": f"root-{project_id}",
            "view_url": f"https://next.frame.io/project/{project_id}",
            "updated_at": "2026-11-01T00:00:00Z",
        }
        self.shares.setdefault(project_id, [])

    def add_share(
        self,
        project_id: str,
        share_id: str,
        name: str,
        *,
        enabled: bool = True,
        expiration: str | None = None,
        last_viewed_at: str | None = None,
        created_at: str | None = None,
        file_ids: tuple[str, ...] = (),
    ) -> None:
        self.shares[project_id].append(
            {
                "inserted_at": created_at,
                "asset_ids": list(file_ids),
                "id": share_id,
                "name": name,
                "short_url": f"https://f.io/{share_id}",
                "enabled": enabled,
                "expiration": expiration,
                "last_viewed_at": last_viewed_at,
            }
        )
        self.activities.setdefault(share_id, [])

    def add_activity(self, share_id: str, inserted_at: str, kind: str = "share_viewed") -> None:
        entries = self.activities[share_id]
        entries.append({"id": f"{share_id}-a{len(entries) + 1}", "type": kind, "inserted_at": inserted_at})

    def add_file(self, file_id: str, name: str, parent_id: str = "folder-1") -> None:
        self.files[file_id] = {
            "id": file_id,
            "name": name,
            "parent_id": parent_id,
            "view_url": f"https://next.frame.io/file/{file_id}",
            "created_at": "2026-11-01T00:00:00Z",
        }

    def add_version_stack(self, stack_id: str, file_ids: list[str]) -> None:
        """Put files that exist into one version stack, oldest first."""
        self.stacks[stack_id] = list(file_ids)
        for file_id in file_ids:
            self.files[file_id]["parent_id"] = stack_id

    def add_comment(
        self,
        file_id: str,
        comment_id: str,
        text: str,
        author: str = "Reviewer One",
        *,
        author_id: str = "u1",
        created_at: str = "2026-11-02T09:00:00Z",
    ) -> None:
        self.comments.setdefault(file_id, []).append(
            {
                "id": comment_id,
                "file_id": file_id,
                "text": text,
                "owner": {"id": author_id, "name": author},
                "created_at": created_at,
                "completed_at": None,
            }
        )

    def set_status(self, file_id: str, *values: str) -> None:
        """Give a file Frame.io's `Status` field next to an unrelated text field."""
        self.metadata[file_id] = [
            {"field_type": "text", "field_definition_name": "Notes", "value": "made-up note"},
            {
                "field_type": "select",
                "field_definition_name": "Status",
                "value": [{"display_name": value, "id": value} for value in values],
            },
        ]

    def refuse_next(self, status: int, headers: Mapping[str, str] | None = None, *, times: int = 1) -> None:
        self._refusals.extend([(status, headers or {})] * times)

    # The HTTP side

    def fetch(self, url: str, headers: Mapping[str, str]) -> tuple[int, Mapping[str, str], JSON]:
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parts.query)
        self.requests.append(parts.path)
        self.tokens_seen.append(headers.get("Authorization", ""))
        if (parts.scheme, parts.netloc) != ("https", "api.frame.io"):
            return 404, {}, {}
        if self._refusals:
            status, answered = self._refusals.pop(0)
            return status, answered, {"errors": [{"detail": "refused"}]}
        prefix = f"/v4/accounts/{ACCOUNT}"
        if parts.path == "/v4/accounts":
            return self._page(parts.path, query, self.accounts)
        if not parts.path.startswith(prefix + "/"):
            return 404, {}, {}
        rest = parts.path[len(prefix) :]
        for pattern, answer in (
            (r"/projects", lambda: list(self.projects.values())),
            (r"/projects/([^/]+)/shares", lambda key: self.shares.get(key)),
            (r"/shares/([^/]+)/activities", lambda key: self.activities.get(key)),
            (r"/files/([^/]+)/comments", lambda key: self.comments.get(key, []) if key in self.files else None),
            (
                r"/version_stacks/([^/]+)/children",
                lambda key: [self.files[file_id] for file_id in self.stacks[key]] if key in self.stacks else None,
            ),
        ):
            match = re.fullmatch(pattern, rest)
            if match:
                rows = answer(*match.groups())
                return (404, {}, {}) if rows is None else self._page(parts.path, query, rows)
        match = re.fullmatch(r"/comments/([^/]+)", rest)
        if match:
            found = [c for rows in self.comments.values() for c in rows if c["id"] == match.group(1)]
            return (200, {}, {"data": found[0]}) if found else (404, {}, {})
        match = re.fullmatch(r"/files/([^/]+)(/metadata)?", rest)
        if match and match.group(1) in self.files:
            file_id = match.group(1)
            if match.group(2):
                return 200, {}, {"data": {"file_id": file_id, "metadata": self.metadata.get(file_id, [])}}
            return 200, {}, {"data": self.files[file_id]}
        return 404, {}, {}

    def _page(self, path: str, query: Mapping[str, list[str]], rows: list[JSON]) -> tuple[int, Mapping[str, str], JSON]:
        start = int(query.get("after", ["0"])[0])
        end = start + self._page_size
        following = f"{path}?after={end}" if end < len(rows) else None
        return 200, {}, {"data": rows[start:end], "links": {"next": following}}
