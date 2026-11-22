"""The guest reviewer's name, read from a public Frame.io share page (#545).

A comment a guest left through a share link has no `owner` in the API. The share page the guest used can carry the
name: in the JSON state embedded in its `<script>` tags. Reading it is best effort. Frame.io does not document this
state, so every failure (no page, bad HTML, no match) answers None and the caller keeps its guest fallback.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from html.parser import HTMLParser
import json
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

PAGE_HOSTS = ("f.io", "frame.io")
"""A share page is read only on these hosts or their subdomains, so a URL from an API answer cannot point the worker elsewhere."""
PAGE_TIMEOUT_SECONDS = 5.0
PAGE_LIMIT_BYTES = 2_000_000
NAME_LIMIT = 80
_NAME_PARENTS = ("owner", "author", "user", "guest", "creator")
_NAME_KEYS = ("display_name", "name", "author_name", "guest_name")

PageFetch = Callable[[str], str | None]


def _frame_io_host(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host == known or host.endswith("." + known) for known in PAGE_HOSTS)


def fetch_share_page(url: str) -> str | None:
    """The HTML of a share page, or None when the URL is not a Frame.io one, the read fails or the page is too large."""
    if not _frame_io_host(url):
        return None
    try:
        with urllib.request.urlopen(url, timeout=PAGE_TIMEOUT_SECONDS) as response:  # noqa: S310 - https and host checked
            if not _frame_io_host(response.geturl()):  # a redirect must stay on Frame.io
                return None
            raw = response.read(PAGE_LIMIT_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return raw.decode("utf-8", errors="replace") if len(raw) <= PAGE_LIMIT_BYTES else None


class _Scripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.blocks: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self._in_script = True
            self.blocks.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self.blocks[-1] += data


def _objects(value: Any) -> Iterator[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)


def _clean(name: Any) -> str | None:
    if not isinstance(name, str):
        return None
    text = "".join(ch for ch in name if ch.isprintable()).strip()
    return text[:NAME_LIMIT] or None


def _name_of(comment: Mapping[str, Any]) -> str | None:
    for parent in _NAME_PARENTS:
        holder = comment.get(parent)
        if isinstance(holder, Mapping):
            for key in _NAME_KEYS:
                if (found := _clean(holder.get(key))) is not None:
                    return found
    for key in ("author_name", "guest_name"):
        if (found := _clean(comment.get(key))) is not None:
            return found
    return None


def guest_name(html: str, comment_id: str) -> str | None:
    """The name the page's embedded JSON gives the comment `comment_id`, or None. Only that comment's own entry counts."""
    parser = _Scripts()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - the parser is best effort: broken HTML is "no name"
        return None
    for block in parser.blocks:
        try:
            state = json.loads(block)
        except ValueError:
            continue
        for candidate in _objects(state):
            if candidate.get("id") == comment_id and (found := _name_of(candidate)) is not None:
                return found
    return None
