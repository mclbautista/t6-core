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

What is hidden, and how IDs come back, is set per task by :class:`MaskOptions`.
The ``names`` preset (the default) hides the registered names, projects and
IDs. The ``strict`` preset also hides single name words, any ID or link found
in the text, and hides ordinary words only when capitalised.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Iterable, Sequence
from enum import Enum
from typing import Any, Literal, TypeVar, cast

from t6.privacy.words import MONTH_WORDS, PROJECT_PLAIN_WORDS, is_ordinary

Kind = Literal["person", "project", "id"]
T = TypeVar("T")

# Any token shaped like a placeholder, issued or not: "[person 1]", "PERSON 1", "[ person1 ]".
_PLACEHOLDER = re.compile(r"(?<!\w)(?:\[\s*)?(person|project|id)\s*(\d+)\b(?:\s*\])?", re.I)


# An ID or link found in the text, whether or not it was registered.
_DETECTED_IDS = (
    r"https?://\S*(?:notion\.(?:so|site|com)|t\.me)\S*"
    r"|\b(?:notion\.(?:so|site|com)|t\.me)/\S*"
    r"|\btg://\S*"
    r"|\b[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}\b"
    r"|(?<![\w@])@[A-Za-z0-9_]{3,}"
    r"|(?<!\d)(?<!\d[.,])\d{7,}(?!\d)(?![.,]\d)"
)


@dataclasses.dataclass(frozen=True)
class MaskOptions:
    """What one task hides, and how IDs come back. Set per task; ``NAMES`` and ``STRICT`` are the presets."""

    names: bool = True
    """Whole registered names: people, their aliases and handles."""
    name_words: bool = False
    """Single words of a person's name, when 3 letters or more and not a month name."""
    projects: bool = True
    """Project names and codes."""
    ids: bool = False
    """IDs and links found in the text: Notion and t.me links, tg:// links, UUIDs, @handles, runs of 7 or
    more digits. Registered IDs (``Registry.ids``) are always hidden."""
    id_restore: Literal["original", "[id]"] = "original"
    """What a restored ID becomes: the original text, or the fixed text ``[id]``."""
    ordinary_words: Literal["hidden", "capitalised"] = "hidden"
    """``capitalised``: a name word or Project code that is an ordinary word is hidden only as written,
    capitalised or in capitals, so "will" and "two" stay readable while "Will" and "TWO" are hidden."""
    skip_trivial: bool = False
    """Leave alone aliases and Project codes of one character, and Project names of under four letters
    or that are an ordinary word ("Home", "Live"), since they are far more often plain words."""


NAMES = MaskOptions()
"""Hide registered names, projects and IDs. The default."""

STRICT = MaskOptions(
    name_words=True, ids=True, id_restore="[id]", ordinary_words="capitalised", skip_trivial=True
)
"""The notion-reports redactor: also single name words and any ID or link in the text."""

PRESETS: dict[str, MaskOptions] = {"names": NAMES, "strict": STRICT}


def _options(options: MaskOptions | str) -> MaskOptions:
    if isinstance(options, MaskOptions):
        return options
    if options not in PRESETS:
        raise ValueError(f"unknown masking preset {options!r}; use one of {', '.join(PRESETS)}")
    return PRESETS[options]


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


@dataclasses.dataclass(frozen=True)
class _Term:
    kind: Kind
    name: str
    loose: bool = True
    """Any case when true, else only as written, capitalised or in capitals."""
    is_handle: bool = False


def _terms(registry: Registry, options: MaskOptions) -> list[_Term]:
    """The names to hide under ``options``. The first kind to claim a name keeps it: person, project, ID."""
    cautious = options.ordinary_words == "capitalised"
    found: dict[str, _Term] = {}

    def add(term: _Term) -> None:
        key = _key(term.name)
        if not key:
            return
        known = found.get(key)
        if known is None:
            found[key] = term
        elif known.kind == term.kind and not (known.loose and term.loose):
            # One name from a full name and from a partial word: the stricter reading of "full" wins.
            found[key] = dataclasses.replace(known, loose=known.loose or term.loose)

    words: list[_Term] = []
    for person in registry.people:
        if options.names:
            for name in (person.name, *person.aliases):
                if len(name.strip()) >= (2 if options.skip_trivial and name != person.name else 1):
                    add(_Term("person", name))
            for handle in person.handles:
                add(_Term("person", handle.lstrip("@"), is_handle=True))
        if options.name_words:
            for word in person.name.split():
                if len(word) >= 3 and word.casefold() not in MONTH_WORDS and _key(word) != _key(person.name):
                    words.append(_Term("person", word, loose=not (cautious and is_ordinary(word))))
    # A partial word never weakens a full name, whatever order the people came in.
    full = {_key(t.name) for t in found.values()}
    for term in words:
        if _key(term.name) not in full:
            add(term)
    if options.projects:
        for project in registry.projects:
            name = project.name.strip()
            trivial = len(name) < 4 or name.casefold() in PROJECT_PLAIN_WORDS or is_ordinary(name)
            if name and not (options.skip_trivial and trivial):
                add(_Term("project", project.name))
            code = (project.code or "").strip()
            if code and not (options.skip_trivial and len(code) < 2):
                add(_Term("project", code, loose=not (cautious and is_ordinary(code))))
    for known in registry.ids:
        add(_Term("id", known))
    return list(found.values())


def _source(term: _Term, options: MaskOptions) -> str:
    """The pattern for one name: whole word, with the case rule and, for a number, no decimal reading."""
    if term.loose:
        body = r"\s+".join(re.escape(word) for word in _key(term.name).split())
        body = f"(?i:{body})"
    else:
        forms = dict.fromkeys(re.escape(f) for f in (term.name, term.name.capitalize(), term.name.upper()))
        body = "(?:" + "|".join(forms) + ")"
    guard_start = guard_end = ""
    if options.ids and term.kind == "id" and term.name.lstrip("-").isdigit():
        guard_start, guard_end = r"(?<!\d[.,])", r"(?![.,]\d)"
    return rf"(?<!\w){guard_start}{'@?' if term.is_handle else ''}{body}(?!\w){guard_end}"


class _Matcher:
    """One leftmost, longest-first pattern over the names to hide, and over detected IDs when asked."""

    def __init__(self, terms: Iterable[_Term], options: MaskOptions, *, detect_ids: bool = False) -> None:
        self.kinds: dict[str, Kind] = {_key(t.name): t.kind for t in terms}
        ordered = sorted(terms, key=lambda t: len(_key(t.name)), reverse=True)
        sources = [_source(t, options) for t in ordered]
        if detect_ids:
            sources.insert(0, f"(?P<detected>(?i:{_DETECTED_IDS}))")
        self.pattern = re.compile("|".join(sources)) if sources else None

    def kind_of(self, match: re.Match[str]) -> Kind:
        """What a match is. One the ID patterns made is an ID, whatever a registered name says."""
        if "detected" in match.re.groupindex and match.group("detected") is not None:
            return "id"
        return self.kinds.get(_key(match.group(0)), "id")

    def finditer(self, text: str) -> Iterable[re.Match[str]]:
        return self.pattern.finditer(text) if self.pattern else ()

    def sub(self, replace: Callable[[re.Match[str]], str], text: str) -> str:
        return self.pattern.sub(replace, text) if self.pattern else text


class Session:
    """The local placeholder map for one masked payload. Never send it to a provider."""

    def __init__(
        self, registry: Registry, allow: Iterable[str] = (), options: MaskOptions | str = NAMES
    ) -> None:
        self._options = _options(options)
        allowed = {_key(name) for name in allow}
        self._allowed = allowed
        # Allowed names stay in the pattern so that a shorter name inside one is not masked.
        terms = _terms(registry, self._options)
        self._matcher = _Matcher(terms, self._options, detect_ids=self._options.ids)
        # Leaks look for names and registered IDs only, not for anything shaped like an ID.
        self._finder = _Matcher(terms, self._options)
        self._originals: dict[str, str] = {}
        self._by_value: dict[tuple[Kind, str], str] = {}
        self._counts: dict[Kind, int] = {"person": 0, "project": 0, "id": 0}

    def _mask(self, text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            found = match.group(0)
            key = _key(found)
            if key in self._allowed:
                return found
            kind = self._matcher.kind_of(match)
            value = (kind, key)
            if value not in self._by_value:
                self._counts[kind] += 1
                token = f"[{kind} {self._counts[kind]}]"
                self._by_value[value] = token
                self._originals[token] = found
            return self._by_value[value]

        return self._matcher.sub(replace, text)

    def mask(self, text: str) -> Masked:
        """Mask one more text with this session's placeholders, for inputs with several text fields.

        A name seen before keeps its placeholder, so ``restore`` works across every field.
        """
        return Masked(self._mask(text))

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
                    if match.group(1).lower() == "id" and self._options.id_restore == "[id]":
                        return "[id]"
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
                for match in self._finder.finditer(text)
                if _key(match.group(0)) not in self._allowed
            )
            return text

        _map_strings(output, "", check)
        return found


def pseudonymize(
    text: str, registry: Registry, allow: Iterable[str] = (), options: MaskOptions | str = NAMES
) -> tuple[Masked, Session]:
    """Replace registry names in ``text`` with placeholders; names in ``allow`` stay as written.

    ``options`` says what to hide and how IDs come back: a :class:`MaskOptions`, or the preset name
    ``"names"`` (the default) or ``"strict"``.
    Two casings of one name share one placeholder. Restore gives back the first casing seen.
    """
    session = Session(registry, allow, options)
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
