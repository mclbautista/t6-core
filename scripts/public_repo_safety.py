#!/usr/bin/env python3
"""Stop secrets and client data from reaching a public T6 repository.

Run from the root of any git repository:

    python3 scripts/public_repo_safety.py

Three checks run over tracked files only:

1. Forbidden paths: env files, databases, real configs, data folders and
   token stores.
2. Names: no tracked line may contain a name from the denylist. The denylist
   comes from the T6_NAMES_DENYLIST environment variable (one name per line)
   or --names-file. Matches print the file and line, never the name.
3. Secrets: gitleaks scans commits (the given --range, the commits not yet
   pushed, or --full-history). Set T6_GITLEAKS to use a specific binary.

The script uses only the standard library so any repository can run it.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Sequence
from fnmatch import fnmatch
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys

GITLEAKS_VERSION = "8.21.2"

ALLOWED_NAMES = {".env.example", "config.example.yaml", "config.example.yml"}
FORBIDDEN_NAMES = [
    ".env",
    ".env.*",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "config.yaml",
    "config.yml",
    "token.json",
    "tokens.json",
    "*_token.json",
    "*_tokens.json",
    "*-token.json",
    "*-tokens.json",
    "*.token",
]
FORBIDDEN_DIRECTORIES = {"data"}


def forbidden_paths(paths: Iterable[str]) -> list[str]:
    """Return the tracked paths that must never be committed."""
    found = []
    for path in paths:
        parts = PurePosixPath(path).parts
        name = parts[-1]
        if FORBIDDEN_DIRECTORIES.intersection(parts[:-1]):
            found.append(path)
        elif name not in ALLOWED_NAMES and any(fnmatch(name, p) for p in FORBIDDEN_NAMES):
            found.append(path)
    return found


def load_denylist(names_file: str | None) -> list[str]:
    if names_file:
        text = Path(names_file).read_text(encoding="utf-8")
    else:
        text = os.environ.get("T6_NAMES_DENYLIST", "")
    names = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.append(line)
    return names


def name_hits(root: Path, paths: Iterable[str], names: Sequence[str]) -> list[str]:
    """Return "file:line" for every tracked line that contains a denied name."""
    alternatives = "|".join(
        r"\s+".join(re.escape(word) for word in name.split()) for name in names
    )
    pattern = re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE)
    hits = []
    for path in paths:
        try:
            content = (root / path).read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            continue
        if b"\x00" in content:
            continue
        text = content.decode("utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                hits.append(f"{path}:{number}")
    return hits


def default_range(root: Path) -> str | None:
    """Commits not yet on the upstream branch, or None for the whole history."""
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return f"{result.stdout.strip()}..HEAD"


def scan_secrets(root: Path, log_range: str | None) -> bool:
    gitleaks = os.environ.get("T6_GITLEAKS") or shutil.which("gitleaks")
    if not gitleaks or not Path(gitleaks).exists():
        print(
            f"FAIL secrets: gitleaks {GITLEAKS_VERSION} is not installed. Install it "
            "(https://github.com/gitleaks/gitleaks#installing) or pass --skip-secrets."
        )
        return False
    command = [gitleaks, "git", "--redact", "--no-banner", "--exit-code", "1"]
    if log_range:
        command.append(f"--log-opts={log_range}")
    command.append(str(root))
    scope = log_range or "full history"
    if subprocess.run(command, cwd=root).returncode != 0:
        print(f"FAIL secrets: gitleaks found likely secrets in {scope}. Rotate them, then remove them.")
        return False
    print(f"ok   secrets: gitleaks found nothing in {scope}")
    return True


def tracked_files(root: Path) -> list[str]:
    output = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True
    ).stdout
    return [path for path in output.decode("utf-8").split("\0") if path]


def parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--range", help="git revision range for the secret scan, e.g. main..HEAD")
    scope.add_argument("--full-history", action="store_true", help="scan every commit for secrets")
    parser.add_argument("--skip-secrets", action="store_true", help="skip the gitleaks scan")
    parser.add_argument("--names-file", help="read the names denylist from this file")
    parser.add_argument(
        "--require-names",
        action="store_true",
        help="fail when no denylist is configured (used in CI)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True
        ).stdout.strip()
    )
    paths = tracked_files(root)
    passed = True

    bad_paths = forbidden_paths(paths)
    for path in bad_paths:
        print(f"FAIL forbidden path: {path}")
    if bad_paths:
        passed = False
    else:
        print("ok   forbidden paths")

    names = load_denylist(arguments.names_file)
    if not names:
        if arguments.require_names:
            print("FAIL names: no denylist. Set the T6_NAMES_DENYLIST secret.")
            passed = False
        else:
            print("skip names check skipped: set T6_NAMES_DENYLIST or pass --names-file")
    else:
        hits = name_hits(root, paths, names)
        for hit in hits:
            print(f"FAIL denied name at {hit}")
        if hits:
            passed = False
        else:
            print(f"ok   names ({len(names)} on the denylist)")

    if not arguments.skip_secrets:
        log_range = None if arguments.full_history else arguments.range or default_range(root)
        passed = scan_secrets(root, log_range) and passed

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
