"""Normalizers are configuration, not code paths.

Ported in spirit from the watchdog's `jo_watchdog/core/resolve.py` and the
gateway's `notion.py`: handles lose `@` and case, names lose honorifics
(ate, kuya, sir, ...), codes lose case and separators, hostnames lose a
local domain suffix.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

DEFAULT_HONORIFICS: frozenset[str] = frozenset(
    {"ate", "kuya", "sir", "maam", "mam", "madam", "mr", "mrs", "ms", "miss", "atty", "engr", "direk", "dir", "tito", "tita"}
)
"""Leading words dropped from a name, already in `Normalizers.text` form."""

_NOT_ALNUM = re.compile(r"[^0-9a-z]+")


@dataclass(frozen=True, slots=True)
class Normalizers:
    honorifics: frozenset[str] = DEFAULT_HONORIFICS
    host_suffixes: tuple[str, ...] = (".local", ".lan", ".home.arpa")

    def text(self, value: str) -> str:
        """Lowercase, accents and apostrophes removed, other punctuation as single spaces."""
        folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
        folded = folded.casefold().replace("'", "").replace("’", "")
        return _NOT_ALNUM.sub(" ", folded).strip()

    def name(self, value: str) -> str:
        """`text`, with leading honorifics dropped (always keeping at least one word)."""
        words = self.text(value).split()
        while len(words) > 1 and words[0] in self.honorifics:
            words.pop(0)
        return " ".join(words)

    def handle(self, value: str) -> str:
        """A Telegram handle without `@`, a `t.me/` prefix or case."""
        text = value.strip().casefold()
        if "t.me/" in text:
            text = text.split("t.me/", 1)[1]
        return text.lstrip("@").strip()

    def code(self, value: str) -> str:
        """A code or key without case, spaces or separators: `BH-101` and `bh 101` agree."""
        return _NOT_ALNUM.sub("", value.casefold())

    def host(self, value: str) -> str:
        """A hostname or serial number without case, a trailing dot or a local suffix."""
        text = value.strip().casefold().rstrip(".")
        for suffix in self.host_suffixes:
            if text.endswith(suffix):
                return text[: -len(suffix)]
        return text
