"""`t6 check-consumer`: find code that goes around t6-core.

Apps must reach Notion, LLM providers and Frame.io through t6-core (ADR 0002).
This scans an app's Python files and reports each place that

- names a Notion, OpenRouter, DeepSeek or Frame.io API host in a string, which
  is how a direct HTTP call starts, or
- imports ``t6.notion.generated``.

Docstrings and comments are not scanned. A file that cannot be parsed is
reported too, since it cannot be shown to be clean.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import re

__all__ = ["Finding", "check"]

SKIPPED_DIRECTORIES = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".tox"})

_HOSTS: dict[str, str] = {
    "Notion": r"api\.notion\.com",
    "OpenRouter": r"openrouter\.ai",
    "DeepSeek": r"api\.deepseek\.com",
    "Frame.io": r"(?:[\w-]+\.)*frame\.io|(?:[\w-]+\.)*adobelogin\.com",
}
_HOST = re.compile(
    r"(?<![\w.-])(?:" + "|".join(f"(?P<{name.replace('.', '_')}>{pattern})" for name, pattern in _HOSTS.items()) + r")(?![\w-])",
    re.IGNORECASE,
)
_GENERATED = "t6.notion.generated"


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    rule: str
    detail: str

    def as_json(self) -> dict[str, object]:
        return {"file": self.file, "line": self.line, "rule": self.rule, "detail": self.detail}


def check(root: Path) -> list[Finding]:
    """Scan ``root`` (a directory or one .py file); findings are ordered by file and line."""
    findings: list[Finding] = []
    for path in _python_files(root):
        name = str(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=name)
        except (SyntaxError, UnicodeDecodeError, ValueError) as error:
            findings.append(Finding(name, getattr(error, "lineno", None) or 1, "unreadable", f"cannot parse: {error}"))
            continue
        findings.extend(_scan(tree, name))
    return sorted(findings, key=lambda f: (f.file, f.line, f.rule))


def _python_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return sorted(p for p in root.rglob("*.py") if not SKIPPED_DIRECTORIES.intersection(p.relative_to(root).parts))


def _docstring_nodes(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                found.add(id(first.value))
    return found


def _scan(tree: ast.AST, file: str) -> list[Finding]:
    docstrings = _docstring_nodes(tree)
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _GENERATED or alias.name.startswith(_GENERATED + "."):
                    findings.append(Finding(file, node.lineno, "generated-import", f"imports {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0 and (
                module == _GENERATED
                or module.startswith(_GENERATED + ".")
                or (module == "t6.notion" and any(alias.name == "generated" for alias in node.names))
            ):
                findings.append(Finding(file, node.lineno, "generated-import", f"imports {_GENERATED}"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            match = _HOST.search(node.value)
            if match:
                service = next(name for name in _HOSTS if match.group(name.replace(".", "_")))
                findings.append(Finding(file, node.lineno, "direct-http", f"names the {service} API host {match.group(0)}"))
    return findings
