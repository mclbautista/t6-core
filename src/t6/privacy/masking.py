"""Reversible masking: swap registry names for placeholders and get them back.

An app fills a :class:`Registry` with the people, projects and known IDs it
holds. :func:`pseudonymize` replaces each registered name in free text with a
numbered placeholder such as ``[person 1]`` and returns the :class:`Masked`
text with the :class:`Session` that issued the placeholders. Only the masked
text leaves the machine. The session stays local.

``Session.restore`` puts the names back into a validated output model. It
accepts placeholder variants that models produce (other case, extra or missing
spaces, missing brackets) and restores only placeholders it issued. If any
placeholder-shaped token is left over, it raises :class:`Unresolved` and
restores nothing: the call fails closed. ``Session.leaks`` reports registry
names that appear raw in an output model.

Privacy never reads Notion. The app decides which fields are free text and
which names to ``allow``.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Iterable, Sequence
from enum import Enum
from typing import Any, Literal, TypeVar, cast

Kind = Literal["person", "project", "id"]
T = TypeVar("T")

# Any token shaped like a placeholder, issued or not: "[person 1]", "PERSON 1", "[ person1 ]".
_PLACEHOLDER = re.compile(r"(?<!\w)(?:\[\s*)?(person|project|id)\s*(\d+)\b(?:\s*\])?", re.I)


@dataclasses.dataclass(frozen=True)
class Person:
    """A person, with every name an app wants hidden: aliases and chat handles."""

    name: str
    aliases: Sequence[str] = ()
    handles: Sequence[str] = ()


@dataclasses.dataclass(frozen=True)
class Project:
    """A project, hidden by its name and by its code."""

    name: str
    code: str | None = None


@dataclasses.dataclass(frozen=True)
class Registry:
    """The names to hide, filled by the app. Matching is by whole word and ignores case."""

    people: Sequence[Person] = ()
    projects: Sequence[Project] = ()
    ids: Sequence[str] = ()

    def terms(self) -> Iterable[tuple[Kind, str, bool]]:
        """Every registered name as ``(kind, name, is_handle)``."""
        for person in self.people:
            for name in (person.name, *person.aliases):
                yield "person", name, False
            for handle in person.handles:
                yield "person", handle.lstrip("@"), True
        for project in self.projects:
            yield "project", project.name, False
            if project.code:
                yield "project", project.code, False
        for known in self.ids:
            yield "id", known, False


@dataclasses.dataclass(frozen=True)
class Masked:
    """Text with registry names replaced by placeholders. Deliberately not a ``str``."""

    text: str


@dataclasses.dataclass(frozen=True)
class Leak:
    """A raw registry name found in an output model, at a field path such as ``changes[0].text``."""

    path: str
    name: str


class Unresolved(Exception):
    """An output model holds placeholder-shaped tokens that the session did not issue."""

    def __init__(self, tokens: Sequence[tuple[str, str]]) -> None:
        self.tokens = tuple(tokens)
        found = ", ".join(f"{token} at {path or '<root>'}" for path, token in self.tokens)
        super().__init__(f"unresolved placeholders: {found}")


def _key(text: str) -> str:
    return " ".join(text.lstrip("@").split()).casefold()


class _Matcher:
    """One longest-first, whole-word pattern over a set of registry names."""

    def __init__(self, terms: Iterable[tuple[Kind, str, bool]]) -> None:
        self.kinds: dict[str, Kind] = {}
        sources: dict[str, str] = {}
        for kind, name, is_handle in terms:
            key = _key(name)
            if not key or key in self.kinds:
                continue
            self.kinds[key] = kind
            body = r"\s+".join(re.escape(word) for word in key.split())
            sources[key] = ("@?" if is_handle else "") + body
        ordered = sorted(sources.items(), key=lambda item: len(item[0]), reverse=True)
        alternation = "|".join(source for _, source in ordered)
        self.pattern = re.compile(rf"(?<!\w)(?:{alternation})(?!\w)", re.I) if ordered else None

    def finditer(self, text: str) -> Iterable[re.Match[str]]:
        return self.pattern.finditer(text) if self.pattern else ()

    def sub(self, replace: Callable[[re.Match[str]], str], text: str) -> str:
        return self.pattern.sub(replace, text) if self.pattern else text


class Session:
    """The local placeholder map for one masked payload. Never send it to a provider."""

    def __init__(self, registry: Registry, allow: Iterable[str] = ()) -> None:
        allowed = {_key(name) for name in allow}
        self._allowed = allowed
        # Allowed names stay in the pattern so that a shorter name inside one is not masked.
        self._matcher = _Matcher(registry.terms())
        self._originals: dict[str, str] = {}
        self._by_value: dict[tuple[Kind, str], str] = {}
        self._counts: dict[Kind, int] = {"person": 0, "project": 0, "id": 0}

    def _mask(self, text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            found = match.group(0)
            key = _key(found)
            if key in self._allowed:
                return found
            kind = self._matcher.kinds[key]
            value = (kind, key)
            if value not in self._by_value:
                self._counts[kind] += 1
                token = f"[{kind} {self._counts[kind]}]"
                self._by_value[value] = token
                self._originals[token] = found
            return self._by_value[value]

        return self._matcher.sub(replace, text)

    def restore(self, output: T) -> T:
        """``output`` with every issued placeholder replaced by its original name.

        Walks strings inside dataclasses, pydantic models, dicts, lists and tuples.
        Raises :class:`Unresolved`, and restores nothing, if any placeholder-shaped
        token is left that this session did not issue.
        """
        unresolved: list[tuple[str, str]] = []

        def restore_text(text: str, path: str) -> str:
            def back(match: re.Match[str]) -> str:
                token = f"[{match.group(1).lower()} {match.group(2)}]"
                if token in self._originals:
                    return self._originals[token]
                unresolved.append((path, match.group(0)))
                return match.group(0)

            return _PLACEHOLDER.sub(back, text)

        restored = _map_strings(output, "", restore_text)
        if unresolved:
            raise Unresolved(unresolved)
        return cast(T, restored)

    def leaks(self, output: object) -> list[Leak]:
        """Every raw registry name, other than allowed ones, in ``output``'s strings."""
        found: list[Leak] = []

        def check(text: str, path: str) -> str:
            found.extend(
                Leak(path, match.group(0))
                for match in self._matcher.finditer(text)
                if _key(match.group(0)) not in self._allowed
            )
            return text

        _map_strings(output, "", check)
        return found


def pseudonymize(text: str, registry: Registry, allow: Iterable[str] = ()) -> tuple[Masked, Session]:
    """Replace registry names in ``text`` with placeholders; names in ``allow`` stay as written.

    Two casings of one name share one placeholder. Restore gives back the first casing seen.
    """
    session = Session(registry, allow)
    return Masked(session._mask(text)), session


def _join(path: str, name: str) -> str:
    return f"{path}.{name}" if path else name


def _map_strings(value: object, path: str, fn: Callable[[str, str], str]) -> object:
    """A copy of ``value`` with ``fn(text, path)`` applied to every string field."""
    if isinstance(value, str) and not isinstance(value, Enum):
        return fn(value, path)
    if isinstance(value, dict):
        return {k: _map_strings(v, _join(path, str(k)), fn) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        items = [_map_strings(v, f"{path}[{i}]", fn) for i, v in enumerate(value)]
        if isinstance(value, list):
            return items
        if hasattr(value, "_fields"):  # namedtuple
            return cast(Any, type(value))(*items)
        return tuple(items)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        changes = {
            f.name: _map_strings(getattr(value, f.name), _join(path, f.name), fn)
            for f in dataclasses.fields(value)
            if f.init
        }
        return dataclasses.replace(value, **changes)
    model_fields = getattr(type(value), "model_fields", None)
    if isinstance(model_fields, dict) and hasattr(value, "model_copy"):  # pydantic v2 model
        update = {name: _map_strings(getattr(value, name), _join(path, name), fn) for name in model_fields}
        return value.model_copy(update=update)
    return value
