"""Skill loader and sandboxed script runner for ``SKILL.md`` bundles.

A skill directory holds a ``SKILL.md`` file with optional YAML frontmatter
under ``t6:`` alongside ``references/``, ``schemas/``, and ``scripts/``.
All relative paths are confined inside the resolved skill root, and scripts
execute as subprocesses with JSON on ``stdin``/``stdout`` in a sanitized
environment stripped of provider API keys and service secrets.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, get_args

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from t6.llm.transport import ToolSpec

OnError = Literal["fail", "return_to_model"]
ON_ERRORS: tuple[OnError, ...] = get_args(OnError)

DEFAULT_MAX_TURNS = 8
DEFAULT_SCRIPT_TIMEOUT = 15.0

_BASE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "HOME", "TMPDIR", "PYTHONPATH", "SYSTEMROOT")
_FORBIDDEN_ENV_KEYS = frozenset({
    "OPENROUTER_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "T6_NOTION_TOKEN",
    "T6_LLM_CONFIG",
    "T6_REGISTRY_SECRET",
    "T6_REGISTRY_READ_SECRET",
    "T6_WORKER_SECRET",
    "T6_FRAMEIO_WORKER_SECRET",
    "T6_FRAMEIO_CALLER_SECRET",
    "T6_DIAGNOSTIC_SECRET",
    "T6_TAP_SECRET",
    "T6_FRAMEIO_WEBHOOK_SECRET",
    "T6_NOTION_WEBHOOK_TOKEN",
    "T6_GATEWAY_SECRET",
})
_FORBIDDEN_ENV_PREFIXES = ("GOOGLE_", "T6_GOOGLE_")

FINAL_TURN_SUBMIT = "submit"
FINAL_TURN_INSTRUCTION = (
    "This is the last turn and no tools are available. Hand in your answer now as the required JSON, "
    'and mark anything you could not settle as "(?)".'
)

_T6_KEYS = frozenset({"references", "output_schema", "max_turns", "final_turn", "env", "pre", "tools", "post"})
_HOOK_KEYS = frozenset({"name", "script", "timeout", "on_error"})
_TOOL_KEYS = frozenset({"name", "description", "script", "parameters", "timeout", "on_error"})

_YAML_TRUE = frozenset({"true", "True", "TRUE", "yes", "Yes", "YES", "on", "On", "ON"})
_YAML_FALSE = frozenset({"false", "False", "FALSE", "no", "No", "NO", "off", "Off", "OFF"})
_YAML_NULL = frozenset({"null", "Null", "NULL", "~"})
_INT_RE = re.compile(r"[+-]?(?:0|[1-9][0-9]*)")
_OCT_RE = re.compile(r"0[0-7]+")
_HEX_RE = re.compile(r"0x[0-9a-fA-F]+")
_FLOAT_RE = re.compile(r"(?:[+-]?(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][+-][0-9]+)?|[+-]?[0-9]+[eE][+-][0-9]+)")
_BLOCK_SCALAR_RE = re.compile(r"([|>])([+-])?")
_SIMPLE_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "a": "\a",
    "b": "\b",
    "e": "\x1b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "0": "\0",
    " ": " ",
}


class SkillError(ValueError):
    """Raised for malformed ``SKILL.md`` frontmatter, unknown ``t6`` keys, missing files, or path traversal."""


def _validate_timeout(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise SkillError(f"{label} timeout must be a positive finite number, got {value!r}")
    return float(value)


def _validate_on_error(value: object, *, label: str) -> OnError:
    if value not in ON_ERRORS:
        raise SkillError(f"{label} on_error must be one of {', '.join(ON_ERRORS)}, got {value!r}")
    return value  # type: ignore[return-value]


@dataclasses.dataclass(frozen=True)
class ScriptHook:
    """A pre- or post-processing script declared under ``t6.pre`` or ``t6.post``."""

    name: str
    script: Path
    timeout: float = DEFAULT_SCRIPT_TIMEOUT
    on_error: OnError = "fail"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise SkillError("script hook name must be a non-empty string")
        _validate_timeout(self.timeout, label=f"hook {self.name!r}")
        _validate_on_error(self.on_error, label=f"hook {self.name!r}")


@dataclasses.dataclass(frozen=True)
class ToolScript:
    """A tool script declared under ``t6.tools``."""

    name: str
    description: str
    script: Path
    parameters: Mapping[str, Any]
    timeout: float = DEFAULT_SCRIPT_TIMEOUT
    on_error: OnError = "return_to_model"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise SkillError("tool name must be a non-empty string")
        if not isinstance(self.parameters, dict):
            object.__setattr__(self, "parameters", dict(self.parameters))
        _validate_timeout(self.timeout, label=f"tool {self.name!r}")
        _validate_on_error(self.on_error, label=f"tool {self.name!r}")

    def to_spec(self) -> ToolSpec:
        """Convert this tool script declaration into a transport :class:`~t6.llm.transport.ToolSpec`."""
        return ToolSpec(name=self.name, description=self.description, parameters=self.parameters)


@dataclasses.dataclass(frozen=True)
class ScriptOutcome:
    """Result of executing a skill script subprocess."""

    ok: bool
    stdout: str
    exit_code: int
    duration_ms: int
    error: str = ""


@dataclasses.dataclass(frozen=True)
class Skill:
    """A loaded ``SKILL.md`` bundle with validated references, schema, and scripts."""

    name: str
    description: str
    root: Path
    instructions: str
    references: tuple[tuple[str, str], ...] = ()
    output_schema: dict[str, Any] | None = None
    max_turns: int = DEFAULT_MAX_TURNS
    final_turn: str | None = None
    env: tuple[str, ...] = ()
    pre: tuple[ScriptHook, ...] = ()
    tools: tuple[ToolScript, ...] = ()
    post: tuple[ScriptHook, ...] = ()
    script_digests: tuple[tuple[str, str], ...] = dataclasses.field(default=(), repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.max_turns, bool) or not isinstance(self.max_turns, int) or self.max_turns < 1:
            raise SkillError(f"max_turns must be a positive integer, got {self.max_turns!r}")

    @property
    def skill_hash(self) -> str:
        """Canonical SHA-256 hex digest of instructions, references, schema, and script contents."""
        digests = dict(self.script_digests)
        if not digests and (self.pre or self.tools or self.post):
            for script_rel in _unique_scripts(self.pre, self.tools, self.post):
                candidate = (self.root / script_rel).resolve()
                data = candidate.read_bytes() if candidate.is_file() and candidate.is_relative_to(self.root) else b""
                digests[script_rel.as_posix()] = hashlib.sha256(data).hexdigest()
        canonical: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "instructions": self.instructions,
            "references": [[path, text] for path, text in self.references],
            "output_schema": self.output_schema,
            "max_turns": self.max_turns,
            "env": list(self.env),
            "pre": [
                {"name": h.name, "script": h.script.as_posix(), "timeout": float(h.timeout), "on_error": h.on_error}
                for h in self.pre
            ],
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "script": t.script.as_posix(),
                    "parameters": t.parameters,
                    "timeout": float(t.timeout),
                    "on_error": t.on_error,
                }
                for t in self.tools
            ],
            "post": [
                {"name": h.name, "script": h.script.as_posix(), "timeout": float(h.timeout), "on_error": h.on_error}
                for h in self.post
            ],
            "scripts": [[path, digests[path]] for path in sorted(digests)],
        }
        if self.final_turn is not None:
            canonical["final_turn"] = self.final_turn
        encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def system_prompt(self) -> str:
        """Return the ``SKILL.md`` instructions combined with any loaded reference documents."""
        parts = [self.instructions] if self.instructions else []
        for path, content in self.references:
            parts.append(f"## Reference: {path}\n\n{content}")
        return "\n\n".join(parts)

    @classmethod
    def load(cls, skill_dir: str | Path) -> Skill:
        """Load and validate a skill directory from ``skill_dir/SKILL.md``."""
        root = Path(skill_dir).resolve()
        if not root.is_dir():
            raise SkillError(f"skill directory not found: {skill_dir}")
        skill_md_rel, skill_md = _resolve_confined(root, "SKILL.md", label="SKILL.md")
        del skill_md_rel
        raw_text = _read_utf8(skill_md, label="SKILL.md")
        frontmatter_text, body = _split_frontmatter(raw_text)
        frontmatter = _parse_yaml_mapping(frontmatter_text)

        name = _parse_top_name(frontmatter.get("name"), default=root.name)
        description = _parse_top_description(frontmatter.get("description"))
        t6 = _parse_t6_section(frontmatter.get("t6"))

        references = _load_references(root, t6.get("references"))
        output_schema = _load_output_schema(root, t6.get("output_schema"))
        max_turns = _parse_max_turns(t6.get("max_turns"))
        final_turn = _parse_final_turn(t6.get("final_turn"))
        env = _parse_env_names(t6.get("env"))
        pre = _parse_hooks(t6.get("pre"), root=root, section="pre")
        tools = _parse_tools(t6.get("tools"), root=root)
        post = _parse_hooks(t6.get("post"), root=root, section="post")

        digests = tuple(
            (rel.as_posix(), hashlib.sha256((root / rel).read_bytes()).hexdigest())
            for rel in _unique_scripts(pre, tools, post)
        )
        return cls(
            name=name,
            description=description,
            root=root,
            instructions=body,
            references=references,
            output_schema=output_schema,
            max_turns=max_turns,
            final_turn=final_turn,
            env=env,
            pre=pre,
            tools=tools,
            post=post,
            script_digests=digests,
        )


def _unique_scripts(
    pre: Sequence[ScriptHook], tools: Sequence[ToolScript], post: Sequence[ScriptHook]
) -> list[Path]:
    seen: dict[str, Path] = {}
    entries: tuple[ScriptHook | ToolScript, ...] = (*pre, *tools, *post)
    for entry in entries:
        seen.setdefault(entry.script.as_posix(), entry.script)
    return [seen[key] for key in sorted(seen)]


def _read_utf8(path: Path, *, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise SkillError(f"cannot read {label}: {error}") from error


def _resolve_confined(root: Path, raw_path: object, *, label: str) -> tuple[Path, Path]:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise SkillError(f"{label} path must be a non-empty string")
    candidate = Path(raw_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise SkillError(f"{label} path escapes skill directory: {raw_path!r}")
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise SkillError(f"{label} path escapes skill directory: {raw_path!r}")
    if not resolved.is_file():
        raise SkillError(f"{label} file not found: {raw_path!r}")
    return resolved.relative_to(root), resolved


def _split_frontmatter(raw: str) -> tuple[str, str]:
    text = raw.removeprefix("\ufeff")
    lines = text.splitlines()
    if not lines or lines[0].rstrip() != "---":
        raise SkillError("SKILL.md must start with YAML frontmatter ('---')")
    for idx in range(1, len(lines)):
        if lines[idx].rstrip() in ("---", "..."):
            return "\n".join(lines[1:idx]), "\n".join(lines[idx + 1 :]).strip()
    raise SkillError("SKILL.md frontmatter is missing closing '---'")


def _parse_yaml_mapping(text: str) -> dict[str, Any]:
    try:
        import yaml
    except ImportError:
        parsed = _parse_simple_yaml(text)
    else:
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise SkillError(f"invalid SKILL.md YAML frontmatter: {error}") from error
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise SkillError("SKILL.md frontmatter must be a mapping")
    return {str(key): value for key, value in parsed.items()}


def _parse_top_name(value: object, *, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise SkillError("SKILL.md name must be a non-empty string")
    return value.strip()


def _parse_top_description(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SkillError("SKILL.md description must be a string")
    return value


def _parse_t6_section(raw: object) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise SkillError("t6 frontmatter section must be a mapping")
    result: dict[str, Any] = {}
    unknown: list[str] = []
    for key, value in raw.items():
        if not isinstance(key, str) or key not in _T6_KEYS:
            unknown.append(str(key))
        else:
            result[key] = value
    if unknown:
        raise SkillError(f"unknown key(s) under t6: {', '.join(sorted(unknown))}")
    return result


def _load_references(root: Path, raw: object) -> tuple[tuple[str, str], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise SkillError("t6.references must be a list of relative file paths")
    loaded: list[tuple[str, str]] = []
    for item in raw:
        rel, full = _resolve_confined(root, item, label="t6.references")
        loaded.append((rel.as_posix(), _read_utf8(full, label=f"reference {rel.as_posix()}")))
    return tuple(loaded)


def _load_output_schema(root: Path, raw: object) -> dict[str, Any] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        rel, full = _resolve_confined(root, raw, label="t6.output_schema")
        raw_json = _read_utf8(full, label=f"output_schema {rel.as_posix()}")
        try:
            parsed = json.loads(raw_json)
        except ValueError as error:
            raise SkillError(f"t6.output_schema {rel.as_posix()} is not valid JSON: {error}") from error
        if not isinstance(parsed, dict):
            raise SkillError(f"t6.output_schema {rel.as_posix()} must contain a JSON object")
        schema: dict[str, Any] = parsed
    elif isinstance(raw, Mapping):
        try:
            schema = json.loads(json.dumps({str(k): v for k, v in raw.items()}))
        except (TypeError, ValueError) as error:
            raise SkillError(f"t6.output_schema must be JSON-compatible: {error}") from error
    else:
        raise SkillError("t6.output_schema must be a relative JSON file path or schema mapping")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise SkillError(f"t6.output_schema is not a valid JSON Schema: {error.message}") from error
    return schema


def _parse_max_turns(raw: object) -> int:
    if raw is None:
        return DEFAULT_MAX_TURNS
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise SkillError(f"t6.max_turns must be a positive integer, got {raw!r}")
    return raw


def _parse_final_turn(raw: object) -> str | None:
    if raw is None:
        return None
    if raw != FINAL_TURN_SUBMIT:
        raise SkillError(f"t6.final_turn must be {FINAL_TURN_SUBMIT!r}, got {raw!r}")
    return FINAL_TURN_SUBMIT


def _parse_env_names(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise SkillError("t6.env must be a list of environment variable names")
    names: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip() or item != item.strip() or "=" in item or any(c.isspace() for c in item):
            raise SkillError(f"invalid environment variable name in t6.env: {item!r}")
        names.append(item)
    return tuple(names)


def _check_entry_mapping(entry: object, *, where: str, allowed: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(entry, Mapping):
        raise SkillError(f"{where} entries must be mappings")
    unknown = sorted(str(k) for k in entry if not isinstance(k, str) or k not in allowed)
    if unknown:
        raise SkillError(f"{where}: unknown key(s) {', '.join(unknown)}")
    return entry


def _parse_hooks(raw: object, *, root: Path, section: str) -> tuple[ScriptHook, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise SkillError(f"t6.{section} must be a list of hook definitions")
    hooks: list[ScriptHook] = []
    for idx, entry in enumerate(raw):
        where = f"t6.{section}[{idx}]"
        item = _check_entry_mapping(entry, where=where, allowed=_HOOK_KEYS)
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise SkillError(f"{where}.name must be a non-empty string")
        rel_script, _ = _resolve_confined(root, item.get("script"), label=f"{where}.script")
        timeout = _validate_timeout(item.get("timeout", DEFAULT_SCRIPT_TIMEOUT), label=where)
        on_error = _validate_on_error(item.get("on_error", "fail"), label=where)
        hooks.append(ScriptHook(name=name.strip(), script=rel_script, timeout=timeout, on_error=on_error))
    return tuple(hooks)


def _parse_tools(raw: object, *, root: Path) -> tuple[ToolScript, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise SkillError("t6.tools must be a list of tool definitions")
    tools: list[ToolScript] = []
    seen_names: set[str] = set()
    for idx, entry in enumerate(raw):
        where = f"t6.tools[{idx}]"
        item = _check_entry_mapping(entry, where=where, allowed=_TOOL_KEYS)
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise SkillError(f"{where}.name must be a non-empty string")
        clean_name = name.strip()
        if clean_name in seen_names:
            raise SkillError(f"duplicate tool name {clean_name!r} in t6.tools")
        seen_names.add(clean_name)
        description = item.get("description", "")
        if not isinstance(description, str):
            raise SkillError(f"{where}.description must be a string")
        rel_script, _ = _resolve_confined(root, item.get("script"), label=f"{where}.script")
        raw_params = item.get("parameters")
        if raw_params is None:
            parameters: dict[str, Any] = {"type": "object", "properties": {}}
        elif isinstance(raw_params, Mapping):
            try:
                parameters = json.loads(json.dumps({str(k): v for k, v in raw_params.items()}))
            except (TypeError, ValueError) as error:
                raise SkillError(f"{where}.parameters must be JSON-compatible: {error}") from error
        else:
            raise SkillError(f"{where}.parameters must be a JSON Schema mapping")
        try:
            Draft202012Validator.check_schema(parameters)
        except SchemaError as error:
            raise SkillError(f"{where}.parameters is not a valid JSON Schema: {error.message}") from error
        timeout = _validate_timeout(item.get("timeout", DEFAULT_SCRIPT_TIMEOUT), label=where)
        on_error = _validate_on_error(item.get("on_error", "return_to_model"), label=where)
        tools.append(
            ToolScript(
                name=clean_name,
                description=description,
                script=rel_script,
                parameters=parameters,
                timeout=timeout,
                on_error=on_error,
            )
        )
    return tuple(tools)


def _is_forbidden_env(key: str, blocked: frozenset[str]) -> bool:
    return key in blocked or key.startswith(_FORBIDDEN_ENV_PREFIXES)


def _build_script_env(
    root: Path,
    *,
    env_names: Sequence[str],
    allowed_env: Mapping[str, str] | None,
    blocked_env: Sequence[str],
) -> dict[str, str]:
    blocked = _FORBIDDEN_ENV_KEYS | frozenset(blocked_env)
    env: dict[str, str] = {}
    for key in _BASE_ENV_KEYS:
        if not _is_forbidden_env(key, blocked) and key in os.environ:
            env[key] = os.environ[key]
    env["SKILL_DIR"] = str(root)
    source: Mapping[str, str] = os.environ if allowed_env is None else allowed_env
    for key in env_names:
        if not _is_forbidden_env(key, blocked) and key in source:
            env[key] = source[key]
    for key in list(env):
        if _is_forbidden_env(key, blocked):
            del env[key]
    return env


def run_script(
    script: ScriptHook | ToolScript | Path | str,
    payload: Any,
    *,
    skill_dir: Path | str,
    timeout: float | None = None,
    env_names: Sequence[str] = (),
    allowed_env: Mapping[str, str] | None = None,
    blocked_env: Sequence[str] = (),
) -> ScriptOutcome:
    """Run a skill script as a subprocess with UTF-8 JSON on ``stdin`` and sanitized env."""
    root = Path(skill_dir).resolve()
    if not root.is_dir():
        raise SkillError(f"skill directory not found: {skill_dir}")
    if isinstance(script, (ScriptHook, ToolScript)):
        candidate = script.script
        effective_timeout = script.timeout if timeout is None else _validate_timeout(timeout, label="run_script")
    else:
        candidate = Path(script)
        effective_timeout = DEFAULT_SCRIPT_TIMEOUT if timeout is None else _validate_timeout(timeout, label="run_script")

    joined = candidate if candidate.is_absolute() else (root / candidate)
    resolved = joined.resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise SkillError(f"script path escapes skill directory: {candidate}")
    if not resolved.is_file():
        raise SkillError(f"script file not found: {candidate}")

    cmd = [sys.executable, str(resolved)] if resolved.suffix == ".py" else [str(resolved)]
    stdin_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    env = _build_script_env(root, env_names=env_names, allowed_env=allowed_env, blocked_env=blocked_env)

    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            input=stdin_bytes,
            capture_output=True,
            cwd=root,
            env=env,
            timeout=effective_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        duration_ms = round((time.monotonic() - started) * 1000)
        partial = error.stdout.decode("utf-8", errors="replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
        return ScriptOutcome(
            ok=False,
            stdout=partial,
            exit_code=-1,
            duration_ms=duration_ms,
            error=f"script timed out after {effective_timeout:g}s",
        )
    except OSError as error:
        duration_ms = round((time.monotonic() - started) * 1000)
        return ScriptOutcome(
            ok=False,
            stdout="",
            exit_code=-1,
            duration_ms=duration_ms,
            error=f"{type(error).__name__}: {error}",
        )

    duration_ms = round((time.monotonic() - started) * 1000)
    stdout = proc.stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        return ScriptOutcome(
            ok=False,
            stdout=stdout,
            exit_code=proc.returncode,
            duration_ms=duration_ms,
            error=stderr or f"script exited with code {proc.returncode}",
        )
    return ScriptOutcome(ok=True, stdout=stdout, exit_code=0, duration_ms=duration_ms)


# ---------------------------------------------------------------------------
# Pure-stdlib YAML subset parser (used when PyYAML is not installed)
# ---------------------------------------------------------------------------


def _parse_simple_yaml(text: str) -> Any:
    """Parse YAML frontmatter using stdlib only (mappings, sequences, flow, comments, quotes, scalars)."""
    return _BlockParser(text.splitlines()).parse_root()


def _indent_of(line: str, line_no: int) -> int:
    count = 0
    for ch in line:
        if ch == " ":
            count += 1
        elif ch == "\t":
            raise SkillError(f"tabs are not allowed for YAML indentation (line {line_no})")
        else:
            break
    return count


def _scan_yaml_line(text: str) -> tuple[int, int]:
    """Return ``(end_before_comment, net_flow_delta)`` across ``text`` outside quotes."""
    in_single = False
    in_double = False
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_single:
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    i += 1
                else:
                    in_single = False
        elif in_double:
            if ch == "\\" and i + 1 < n:
                i += 1
            elif ch == '"':
                in_double = False
        elif ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            return i, depth
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        i += 1
    return n, depth


def _strip_inline_comment(text: str) -> str:
    return text[: _scan_yaml_line(text)[0]]


def _decode_quoted(text: str, start: int = 0) -> tuple[str, int]:
    if text[start] == '"':
        return _decode_double_quoted(text, start)
    return _decode_single_quoted(text, start)


def _decode_double_quoted(text: str, start: int = 0) -> tuple[str, int]:
    """Decode a double-quoted YAML scalar starting at ``start`` (index of opening ``\"``)."""
    out: list[str] = []
    i = start + 1
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            return "".join(out), i + 1
        if ch == "\\":
            i += 1
            if i >= n:
                raise SkillError("unterminated escape in double-quoted YAML string")
            esc = text[i]
            if esc in _SIMPLE_ESCAPES:
                out.append(_SIMPLE_ESCAPES[esc])
            elif esc == "x" and i + 2 < n:
                out.append(chr(int(text[i + 1 : i + 3], 16)))
                i += 2
            elif esc == "u" and i + 4 < n:
                out.append(chr(int(text[i + 1 : i + 5], 16)))
                i += 4
            elif esc == "U" and i + 8 < n:
                out.append(chr(int(text[i + 1 : i + 9], 16)))
                i += 8
            else:
                raise SkillError(f"unknown escape \\{esc} in double-quoted YAML string")
        else:
            out.append(ch)
        i += 1
    raise SkillError("unclosed double-quoted YAML string")


def _decode_single_quoted(text: str, start: int = 0) -> tuple[str, int]:
    """Decode a single-quoted YAML scalar starting at ``start`` (index of opening ``'``)."""
    out: list[str] = []
    i = start + 1
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "'":
            if i + 1 < n and text[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    raise SkillError("unclosed single-quoted YAML string")


def _resolve_scalar(token: str) -> Any:
    if token in _YAML_NULL:
        return None
    if token in _YAML_TRUE:
        return True
    if token in _YAML_FALSE:
        return False
    if _INT_RE.fullmatch(token):
        return int(token)
    if _OCT_RE.fullmatch(token):
        return int(token, 8)
    if _HEX_RE.fullmatch(token):
        return int(token, 16)
    if _FLOAT_RE.fullmatch(token):
        return float(token)
    if token in (".inf", ".Inf", ".INF", "+.inf", "+.Inf", "+.INF"):
        return math.inf
    if token in ("-.inf", "-.Inf", "-.INF"):
        return -math.inf
    if token in (".nan", ".NaN", ".NAN"):
        return math.nan
    return token


def _find_mapping_colon(text: str) -> int | None:
    """Return index of key/value ``:`` in a block line, or ``None`` if not a mapping entry."""
    if not text or text.startswith(("{", "[", "-")):
        return None
    if text[0] in ('"', "'"):
        _, end = _decode_quoted(text, 0)
        rest = text[end:].lstrip(" \t")
        if rest.startswith(":") and (len(rest) == 1 or rest[1] in " \t#"):
            return text.index(":", end)
        return None
    for idx, ch in enumerate(text):
        if ch == "#" and (idx == 0 or text[idx - 1] in " \t"):
            return None
        if ch == ":" and (idx + 1 == len(text) or text[idx + 1] in " \t"):
            return idx
    return None


class _FlowParser:
    """Recursive-descent parser for YAML flow mappings ``{...}`` and sequences ``[...]``."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._pos = 0
        self._len = len(text)

    def parse_all(self) -> Any:
        self._skip_ws()
        value = self._parse_value()
        self._skip_ws()
        if self._pos != self._len:
            raise SkillError(f"unexpected trailing text in YAML flow value: {self._text[self._pos:]!r}")
        return value

    def _skip_ws(self) -> None:
        while self._pos < self._len:
            ch = self._text[self._pos]
            if ch in " \t\r\n":
                self._pos += 1
            elif ch == "#" and (self._pos == 0 or self._text[self._pos - 1] in " \t\r\n"):
                while self._pos < self._len and self._text[self._pos] != "\n":
                    self._pos += 1
            else:
                break

    def _parse_value(self) -> Any:
        self._skip_ws()
        if self._pos >= self._len:
            raise SkillError("unexpected end of YAML flow value")
        ch = self._text[self._pos]
        if ch == "{":
            return self._parse_mapping()
        if ch == "[":
            return self._parse_sequence()
        if ch in ('"', "'"):
            val, self._pos = _decode_quoted(self._text, self._pos)
            return val
        return self._parse_plain_scalar(stop_at_colon=False)

    def _parse_mapping(self) -> dict[Any, Any]:
        self._pos += 1  # consume '{'
        result: dict[Any, Any] = {}
        self._skip_ws()
        if self._pos < self._len and self._text[self._pos] == "}":
            self._pos += 1
            return result
        while True:
            self._skip_ws()
            if self._pos < self._len and self._text[self._pos] == "}":
                self._pos += 1
                return result
            key = self._parse_key()
            self._skip_ws()
            if self._pos >= self._len or self._text[self._pos] != ":":
                raise SkillError("expected ':' in YAML flow mapping")
            self._pos += 1
            self._skip_ws()
            if self._pos < self._len and self._text[self._pos] in ",}":
                value: Any = None
            else:
                value = self._parse_value()
            result[key] = value
            self._skip_ws()
            if self._pos < self._len and self._text[self._pos] == ",":
                self._pos += 1
                continue
            if self._pos < self._len and self._text[self._pos] == "}":
                self._pos += 1
                return result
            raise SkillError("expected ',' or '}' in YAML flow mapping")

    def _parse_sequence(self) -> list[Any]:
        self._pos += 1  # consume '['
        items: list[Any] = []
        self._skip_ws()
        if self._pos < self._len and self._text[self._pos] == "]":
            self._pos += 1
            return items
        while True:
            self._skip_ws()
            if self._pos < self._len and self._text[self._pos] == "]":
                self._pos += 1
                return items
            items.append(self._parse_value())
            self._skip_ws()
            if self._pos < self._len and self._text[self._pos] == ",":
                self._pos += 1
                continue
            if self._pos < self._len and self._text[self._pos] == "]":
                self._pos += 1
                return items
            raise SkillError("expected ',' or ']' in YAML flow sequence")

    def _parse_key(self) -> Any:
        ch = self._text[self._pos] if self._pos < self._len else ""
        if ch in ('"', "'"):
            val, self._pos = _decode_quoted(self._text, self._pos)
            return val
        return self._parse_plain_scalar(stop_at_colon=True)

    def _parse_plain_scalar(self, *, stop_at_colon: bool) -> Any:
        start = self._pos
        while self._pos < self._len:
            ch = self._text[self._pos]
            if ch in ",]}#\r\n" and (ch != "#" or self._pos == start or self._text[self._pos - 1] in " \t\r\n"):
                break
            if stop_at_colon and ch == ":":
                break
            self._pos += 1
        token = self._text[start : self._pos].strip()
        if not token:
            raise SkillError("empty scalar in YAML flow collection")
        return _resolve_scalar(token)


class _BlockParser:
    """Line-oriented YAML block parser supporting mappings, sequences, flow, and block scalars."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self._idx = 0

    def parse_root(self) -> Any:
        peek = self._peek()
        if peek is None:
            return None
        _, indent, raw = peek
        stripped = raw[indent:]
        if stripped.startswith(("{", "[")):
            self._idx += 1
            return self._parse_flow_from(stripped)
        if stripped == "-" or stripped.startswith("- "):
            return self._parse_sequence(indent)
        if _find_mapping_colon(stripped) is not None:
            return self._parse_mapping(indent)
        self._idx += 1
        val = self._parse_scalar_text(stripped)
        if self._peek() is not None:
            raise SkillError("unexpected extra content in SKILL.md YAML frontmatter")
        return val

    def _peek(self) -> tuple[int, int, str] | None:
        while self._idx < len(self._lines):
            raw = self._lines[self._idx]
            indent = _indent_of(raw, self._idx + 1)
            stripped = raw[indent:]
            if not stripped or stripped.startswith("#"):
                self._idx += 1
                continue
            return self._idx, indent, raw
        return None

    def _parse_block(self, min_indent: int) -> Any:
        peek = self._peek()
        if peek is None or peek[1] < min_indent:
            return None
        _, indent, raw = peek
        stripped = raw[indent:]
        if stripped == "-" or stripped.startswith("- "):
            return self._parse_sequence(indent)
        if stripped.startswith(("{", "[")):
            self._idx += 1
            return self._parse_flow_from(stripped)
        return self._parse_mapping(indent)

    def _parse_mapping(self, map_indent: int) -> dict[Any, Any]:
        mapping: dict[Any, Any] = {}
        while (peek := self._peek()) is not None:
            idx, indent, raw = peek
            if indent < map_indent:
                break
            if indent > map_indent:
                raise SkillError(f"unexpected indentation in YAML mapping on line {idx + 1}")
            stripped = raw[indent:]
            if stripped == "-" or stripped.startswith("- "):
                break
            self._idx = idx + 1
            key, value = self._parse_mapping_line(stripped, entry_indent=indent, line_no=idx + 1)
            mapping[key] = value
        return mapping

    def _parse_mapping_line(self, content: str, *, entry_indent: int, line_no: int) -> tuple[Any, Any]:
        colon = _find_mapping_colon(content)
        if colon is None:
            raise SkillError(f"expected YAML mapping entry 'key: value' on line {line_no}")
        raw_key = content[:colon].strip()
        if raw_key.startswith(('"', "'")):
            key: Any
            key, _ = _decode_quoted(raw_key, 0)
        else:
            key = _resolve_scalar(raw_key)
        raw_val = content[colon + 1 :].lstrip(" ")
        value = self._parse_value_part(raw_val, parent_indent=entry_indent)
        return key, value

    def _parse_sequence(self, seq_indent: int) -> list[Any]:
        items: list[Any] = []
        while (peek := self._peek()) is not None:
            idx, indent, raw = peek
            if indent < seq_indent:
                break
            if indent > seq_indent:
                raise SkillError(f"unexpected indentation in YAML sequence on line {idx + 1}")
            stripped = raw[indent:]
            if stripped != "-" and not stripped.startswith("- "):
                break
            self._idx = idx + 1
            after_dash = stripped[1:]
            spaces = len(after_dash) - len(after_dash.lstrip(" "))
            rest = after_dash.lstrip(" ")
            rest_clean = _strip_inline_comment(rest).strip()
            if not rest_clean:
                nxt = self._peek()
                items.append(self._parse_block(nxt[1]) if nxt is not None and nxt[1] > seq_indent else None)
            elif rest_clean.startswith(("{", "[")):
                items.append(self._parse_flow_from(rest))
            elif _find_mapping_colon(rest) is not None:
                entry_indent = seq_indent + 1 + spaces
                key, value = self._parse_mapping_line(rest, entry_indent=entry_indent, line_no=idx + 1)
                item: dict[Any, Any] = {key: value}
                nxt = self._peek()
                if nxt is not None and nxt[1] > seq_indent:
                    item.update(self._parse_mapping(nxt[1]))
                items.append(item)
            else:
                items.append(self._parse_value_part(rest, parent_indent=seq_indent))
        return items

    def _parse_value_part(self, raw_val: str, *, parent_indent: int) -> Any:
        cleaned = _strip_inline_comment(raw_val).strip()
        if not cleaned:
            nxt = self._peek()
            if nxt is None:
                return None
            _, next_indent, next_raw = nxt
            next_stripped = next_raw[next_indent:]
            if next_indent > parent_indent:
                return self._parse_block(next_indent)
            if next_indent == parent_indent and (next_stripped == "-" or next_stripped.startswith("- ")):
                return self._parse_sequence(next_indent)
            return None
        block_match = _BLOCK_SCALAR_RE.fullmatch(cleaned)
        if block_match:
            return self._parse_block_scalar(block_match.group(1), block_match.group(2) or "", parent_indent)
        if raw_val.lstrip(" ").startswith(("{", "[")):
            return self._parse_flow_from(raw_val)
        return self._parse_scalar_text(raw_val)

    def _parse_flow_from(self, first_part: str) -> Any:
        chunks = [first_part]
        _, depth = _scan_yaml_line(first_part)
        while depth > 0:
            if self._idx >= len(self._lines):
                raise SkillError("unclosed YAML flow collection")
            line = self._lines[self._idx]
            self._idx += 1
            chunks.append(line)
            depth += _scan_yaml_line(line)[1]
        return _FlowParser("\n".join(chunks)).parse_all()

    def _parse_scalar_text(self, raw_val: str) -> Any:
        text = raw_val.lstrip(" ")
        if text.startswith(('"', "'")):
            val, end = _decode_quoted(text, 0)
            if _strip_inline_comment(text[end:]).strip():
                raise SkillError("unexpected trailing characters after quoted YAML string")
            return val
        token = _strip_inline_comment(text).strip()
        return _resolve_scalar(token)

    def _parse_block_scalar(self, style: str, chomp: str, parent_indent: int) -> str:
        collected: list[str] = []
        content_indent: int | None = None
        while self._idx < len(self._lines):
            raw = self._lines[self._idx]
            if not raw.strip():
                collected.append("")
                self._idx += 1
                continue
            indent = _indent_of(raw, self._idx + 1)
            if content_indent is None:
                if indent <= parent_indent:
                    break
                content_indent = indent
            elif indent < content_indent:
                break
            collected.append(raw[content_indent:])
            self._idx += 1
        while collected and collected[-1] == "" and chomp != "+":
            trailing = collected.pop()
            if chomp == "" and not collected:
                del trailing
                break
        if style == ">":
            text = _fold_block_lines(collected)
        else:
            text = "\n".join(collected)
        if not text and not collected:
            return ""
        if chomp == "-":
            return text
        return text + "\n"


def _fold_block_lines(lines: list[str]) -> str:
    if not lines:
        return ""
    out: list[str] = [lines[0]]
    for prev, curr in zip(lines, lines[1:]):
        if curr == "":
            out.append("\n")
        elif prev == "":
            out.append(curr)
        elif prev.startswith((" ", "\t")) or curr.startswith((" ", "\t")):
            out.append("\n" + curr)
        else:
            out.append(" " + curr)
    return "".join(out)
