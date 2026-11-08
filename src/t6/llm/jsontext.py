"""Lossless clean-up of the ways models mangle JSON replies.

:func:`strip_fence` always runs: a reply wrapped in a Markdown code fence is
unwrapped. :func:`repair_quotes` runs only for tasks that opt in, and only
after the reply failed to parse. Ported from t6-notion-reports ``llm.py``.
"""

from __future__ import annotations

import re

_FENCE = re.compile(r"\s*```(?:json|JSON)?[ \t]*\n?(.*?)\s*```\s*", re.DOTALL)
_AFTER_STRING = frozenset({",", "}", "]", ":"})
_CONTROL = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def strip_fence(text: str) -> str:
    """Return the body of a reply wrapped in one ``` or ```json fence, else ``text`` unchanged."""
    match = _FENCE.fullmatch(text)
    return match.group(1) if match else text


def repair_quotes(text: str) -> str:
    """Escape double quotes (and raw newlines and tabs) that sit inside a JSON string value.

    Inside a string, a quote closes it only if the next non-space character is
    ``, } ] :`` or the text ends; otherwise it is content and gets a backslash.
    Valid escapes are kept. Nothing else is changed.
    """
    out: list[str] = []
    in_string = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if not in_string:
            in_string = ch == '"'
            out.append(ch)
        elif ch == "\\" and i + 1 < n:
            out.append(text[i : i + 2])
            i += 1
        elif ch == '"':
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j >= n or text[j] in _AFTER_STRING:
                in_string = False
                out.append(ch)
            else:
                out.append('\\"')
        elif ch in _CONTROL:
            out.append(_CONTROL[ch])
        else:
            out.append(ch)
        i += 1
    return "".join(out)
