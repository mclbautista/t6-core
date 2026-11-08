#!/usr/bin/env python3
"""Generate `src/t6/notion/generated.py` from the committed schema snapshot.

    uv run python scripts/generate_models.py          # rewrite the module
    uv run python scripts/generate_models.py --check  # exit 1 if it is stale

Each snapshot database becomes one `NotionModel` subclass. A field maps one
Python name to one exact Notion property name, trailing spaces and emoji
included. Names are chosen by `attribute_name` and never depend on the order
of other databases, so adding a property only adds a field.
"""

from __future__ import annotations

import argparse
import json
import keyword
from pathlib import Path
import re
import sys
import unicodedata
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "src" / "t6" / "notion" / "schema_snapshot.json"
OUTPUT = ROOT / "src" / "t6" / "notion" / "generated.py"

RESERVED = {"page_id", "from_page", "to_properties", "notion_fields", "notion_name", "database"}

ANNOTATIONS = {
    "title": ("str | None", "None"),
    "rich_text": ("str | None", "None"),
    "select": ("str | None", "None"),
    "status": ("str | None", "None"),
    "multi_select": ("tuple[str, ...]", "()"),
    "relation": ("tuple[str, ...]", "()"),
    "people": ("tuple[str, ...]", "()"),
    "checkbox": ("bool | None", "None"),
    "number": ("float | int | None", "None"),
    "date": ("dt.date | dt.datetime | None", "None"),
    "url": ("str | None", "None"),
    "email": ("str | None", "None"),
    "phone_number": ("str | None", "None"),
    "created_time": ("dt.datetime | None", "None"),
    "last_edited_time": ("dt.datetime | None", "None"),
}
RAW = ("dict[str, Any] | None", "None")


def attribute_name(notion_name: str, taken: set[str]) -> str:
    """A unique Python identifier for a Notion property name."""
    words = re.sub(r"[^A-Za-z0-9]+", " ", notion_name)
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", words)
    name = "_".join(words.lower().split())
    if not name:  # an emoji-only name: use the characters' Unicode names
        parts = [
            unicodedata.name(char, "").lower().replace(" ", "_")
            for char in notion_name
            if unicodedata.category(char).startswith("S")
        ]
        name = "_".join(part for part in parts if part) or "property"
    if name[0].isdigit():
        name = f"p_{name}"
    if keyword.iskeyword(name) or name in RESERVED:
        name += "_"
    unique, number = name, 1
    while unique in taken:
        unique = f"{name}_{number}"
        number += 1
    taken.add(unique)
    return unique


def class_name(title: str) -> str:
    return "".join(word.capitalize() for word in re.findall(r"[A-Za-z0-9]+", title)) + "Row"


def literal(text: str) -> str:
    """A double-quoted string literal that keeps invisible characters visible."""
    parts = []
    for char in text:
        if char in '"\\':
            parts.append("\\" + char)
        elif 32 <= ord(char) < 127:
            parts.append(char)
        else:
            parts.append(f"\\U{ord(char):08x}")
    return '"' + "".join(parts) + '"'


def render(snapshot: dict[str, Any]) -> str:
    lines = [
        '"""Notion models generated from `schema_snapshot.json`. Do not edit.',
        "",
        "Run `uv run python scripts/generate_models.py` to rebuild this module after the",
        "snapshot changes. Each field maps a Python name to one exact Notion property name.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "import datetime as dt",
        "from dataclasses import dataclass",
        "from typing import Any, ClassVar",
        "",
        "from t6.notion.model import NotionModel, notion",
    ]
    classes: list[tuple[str, str]] = []
    for key, database in snapshot["databases"].items():
        name = class_name(database["title"])
        classes.append((key, name))
        taken: set[str] = set()
        lines += [
            "",
            "",
            "@dataclass(frozen=True, slots=True, kw_only=True)",
            f"class {name}(NotionModel):",
        ]
        lines += [
            f'    """Notion database {database["title"]}."""',
            "",
            f"    DATABASE: ClassVar[str] = {literal(key)}",
        ]
        for prop in database["properties"]:
            annotation, default = ANNOTATIONS.get(prop["type"], RAW)
            attribute = attribute_name(prop["name"], taken)
            lines.append(
                f"    {attribute}: {annotation} = notion("
                f"{literal(prop['name'])}, {literal(prop['type'])}"
                + (f", default={default}" if default != "None" else "")
                + ")"
            )
    lines += ["", "", "MODELS: dict[str, type[NotionModel]] = {"]
    lines += [f"    {literal(key)}: {name}," for key, name in classes]
    lines += ["}", '"""Every generated model, by snapshot database key."""', ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if generated.py is stale")
    args = parser.parse_args()
    expected = render(json.loads(SNAPSHOT.read_text(encoding="utf-8")))
    if args.check:
        if OUTPUT.read_text(encoding="utf-8") != expected:
            print("generated.py is stale: run scripts/generate_models.py", file=sys.stderr)
            return 1
        return 0
    OUTPUT.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
