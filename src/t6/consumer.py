"""Check an app against the consumer contract in ADR 0002."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
import re

__all__ = ["Finding", "check"]

SKIPPED_DIRECTORIES = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".tox"})

_HOSTS: dict[str, str] = {
    "Notion": r"api\.notion\.com",
    "OpenRouter": r"openrouter\.ai",
    "DeepSeek": r"api\.deepseek\.com",
    "Frame.io": r"(?:[\w-]+\.)+frame\.io|(?:[\w-]+\.)+adobelogin\.com",
}
_HOST = re.compile(
    r"(?<![\w.-])(?:" + "|".join(f"(?P<{name.replace('.', '_')}>{pattern})" for name, pattern in _HOSTS.items()) + r")(?![\w-])",
    re.IGNORECASE,
)
_ALLOWLIST = ".t6-consumer-allowlist.json"
_CREDENTIAL = re.compile(
    r"(?<![A-Za-z0-9_])(?:T6_NOTION_TOKEN|(?:OPENAI|ANTHROPIC|OPENROUTER|DEEPSEEK|GEMINI|GOOGLE|MISTRAL|GROQ|COHERE|XAI|AZURE_OPENAI)_(?:API_KEY|TOKEN|SECRET|CREDENTIALS|APPLICATION_CREDENTIALS|CLIENT_SECRET)|GOOGLE_APPLICATION_CREDENTIALS|GOOGLE_CLOUD_(?:API_KEY|CREDENTIALS))(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_CONFIG_SUFFIXES = frozenset({".env", ".yaml", ".yml", ".toml", ".json", ".ini", ".cfg"})


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
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError) as error:
            findings.append(Finding(name, getattr(error, "lineno", None) or 1, "unreadable", f"cannot parse: {error}"))
            continue
        findings.extend(_scan(tree, name))
    for path in _config_files(root):
        try:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                for match in _CREDENTIAL.finditer(line):
                    findings.append(Finding(str(path), number, "credential", f"references {match.group()}"))
        except (OSError, UnicodeDecodeError) as error:
            findings.append(Finding(str(path), 1, "unreadable", f"cannot read: {error}"))
    return sorted(findings, key=lambda f: (f.file, f.line, f.rule))


def _python_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix == ".py" else []
    return sorted(p for p in root.rglob("*.py") if not SKIPPED_DIRECTORIES.intersection(p.relative_to(root).parts))


def _config_files(root: Path) -> list[Path]:
    if root.is_file():
        return [] if root.suffix == ".py" else [root]
    return sorted(p for p in root.rglob("*") if p.is_file() and p.name != _ALLOWLIST
                  and (p.suffix.lower() in _CONFIG_SUFFIXES or p.name.startswith(".env"))
                  and not SKIPPED_DIRECTORIES.intersection(p.relative_to(root).parts))


def apply_allowlist(root: Path, findings: list[Finding]) -> tuple[list[Finding], list[dict[str, str]]]:
    """Apply exact file/rule exceptions; report every entry, including expired ones."""
    base = root if root.is_dir() else root.parent
    path = base / _ALLOWLIST
    if not path.exists():
        return findings, []
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            raise ValueError("expected a list of exceptions")
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"name", "file", "rule", "expires"}:
                raise ValueError("each exception needs name, file, rule and expires")
            if not all(isinstance(value, str) and value.strip() for value in entry.values()):
                raise ValueError("exception fields must be non-empty strings")
            if entry["rule"] not in {"adapter-import", "credential", "direct-http"}:
                raise ValueError("unknown exception rule")
            if Path(entry["file"]).is_absolute() or ".." in Path(entry["file"]).parts:
                raise ValueError("exception file must be relative to the app")
            if date.fromisoformat(entry["expires"]).isoformat() != entry["expires"]:
                raise ValueError("expiry must be YYYY-MM-DD")
    except (OSError, UnicodeDecodeError, ValueError, TypeError) as error:
        return findings + [Finding(str(path), 1, "allowlist", f"invalid allow-list: {error}")], []
    active = []
    for entry in entries:
        if date.fromisoformat(entry["expires"]) < date.today():
            findings.append(Finding(str(path), 1, "allowlist", f"expired exception {entry['name']} ({entry['expires']})"))
        else:
            active.append(entry)
    findings = [f for f in findings if not any(
        f.rule == entry["rule"] and Path(f.file).resolve() == (base / entry["file"]).resolve()
        for entry in active
    )]
    return findings, entries


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
                if alias.name == "t6.notion" or alias.name.startswith("t6.notion.") or alias.name == "t6.llm" or alias.name.startswith("t6.llm."):
                    findings.append(Finding(file, node.lineno, "adapter-import", f"imports {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0:
                modules = [module] + [f"{module}.{alias.name}" for alias in node.names]
                if any(m == "t6.notion" or m.startswith("t6.notion.") or m == "t6.llm" or m.startswith("t6.llm.") for m in modules):
                    findings.append(Finding(file, node.lineno, "adapter-import", f"imports from {module}"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            credential = _CREDENTIAL.search(node.value)
            if credential:
                findings.append(Finding(file, node.lineno, "credential", f"references {credential.group()}"))
            match = _HOST.search(node.value)
            if match:
                service = next(name for name in _HOSTS if match.group(name.replace(".", "_")))
                findings.append(Finding(file, node.lineno, "direct-http", f"names the {service} API host {match.group(0)}"))
    return findings
