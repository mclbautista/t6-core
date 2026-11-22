"""Scan a bay's local DaVinci Resolve disk library, read-only, and print one report as JSON (ADR 0008, #377, #379).

This module is shipped to a bay as one file and run with Munki's own Python
(`/usr/local/munki/munki-python bay_scanner.py`), so it uses the standard library only and imports nothing from `t6`. It must
stay runnable on Python 3.9 and later. A test enforces both. It posts nothing, holds no credential, installs nothing and leaves
nothing behind: the Mac mini collects the report from stdout and posts it to t6 (`bay scan report`).

Exit code 0 means a report was printed, including a skipped one. A real failure exits 1 with the reason on stderr.

The Resolve disk library is a folder of plain SQLite files, one `Project.db` per project (facts in
`docs/research/bay-resolve-disk-library.md`). The reader copies each `Project.db` to a temporary folder, opens only the
copy with `mode=ro&immutable=1`, and deletes the copy. It writes nothing inside the Resolve folders and never starts
Resolve. It does not read `ProjectMetadataCache/Metadata.db`, which is stale and incomplete.
"""
from __future__ import annotations

import argparse
import json
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

LIBRARY_USERS = Path("Library/Application Support/Blackmagic Design/DaVinci Resolve/Resolve Project Library/Resolve Projects/Users")
"""Where the disk library sits below a login user's home. One Resolve user folder per library user."""

PROJECT_DB = "Project.db"

FORMAT_VERSION = 1
"""The report format. `t6.bay_scan.FORMAT_VERSION` is the same number; a contract test keeps them equal."""

SCANNER_VERSION = "1"

RESOLVE_BUNDLE = "/DaVinci Resolve.app/Contents/"
"""A process whose executable path holds this is Resolve (or part of it) and is running."""

RESOLVE_INFO_PLIST = Path("/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Info.plist")


def project_roots(home: Path) -> list[Path]:
    """The `Projects` folder of every Resolve user in the library below `home`; none when there is no library."""
    users = home / LIBRARY_USERS
    if not users.is_dir():
        return []
    return sorted(path / "Projects" for path in users.iterdir() if (path / "Projects").is_dir())


def _read_project(db_copy: Path) -> dict[str, Any]:
    """One project's facts from its copied `Project.db`. Raises `ValueError` with the reason when it cannot be read."""
    try:
        connection = sqlite3.connect(f"{db_copy.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error as error:
        raise ValueError(f"cannot open: {error}") from error
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        missing = sorted({"SM_Project", "Sm2Timeline"} - tables)
        if missing:
            raise ValueError("unknown database layout: no table " + ", ".join(missing))
        rows = connection.execute("SELECT SM_Project_id, ProjectName, LastModTimeInSecs FROM SM_Project").fetchall()
        if len(rows) != 1:
            raise ValueError(f"unknown database layout: {len(rows)} rows in SM_Project, expected 1")
        uuid, name, modified = rows[0]
        if not isinstance(uuid, str) or not isinstance(name, str) or not isinstance(modified, (int, float)):
            raise ValueError("unknown database layout: SM_Project columns hold unexpected types")
        count = connection.execute("SELECT count(*) FROM Sm2Timeline").fetchone()[0]
        return {"project_uuid": uuid, "project_name": name, "last_modified": int(modified), "timeline_count": int(count)}
    except sqlite3.Error as error:
        raise ValueError(f"unreadable: {error}") from error
    finally:
        connection.close()


def scan_library(home: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Every local copy below `home`, and the projects that could not be read, each with a reason.

    A project that fails never stops the others. Nothing under `home` is written: the `Project.db` files are copied to a
    temporary folder that is removed before this returns.
    """
    copies: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    scratch = Path(tempfile.mkdtemp(prefix="t6-bay-scan-"))
    try:
        for root in project_roots(home):
            for source in sorted(root.rglob(PROJECT_DB)):
                relative = source.parent.relative_to(root)
                folder = relative.parent.as_posix()
                folder_path = "" if folder == "." else folder
                try:
                    target = scratch / str(len(copies) + len(errors)) / PROJECT_DB
                    target.parent.mkdir()
                    shutil.copyfile(source, target)
                    copies.append({**_read_project(target), "folder_path": folder_path})
                except (OSError, ValueError) as error:
                    errors.append({"folder_path": folder_path, "project": relative.name, "reason": str(error)})
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    copies.sort(key=lambda copy: (copy["folder_path"], copy["project_name"], copy["project_uuid"]))
    return copies, errors


def running_processes() -> list[str]:
    """The executable path of every running process (`ps -axo comm=`)."""
    done = subprocess.run(["ps", "-axo", "comm="], capture_output=True, text=True, check=True)
    return done.stdout.splitlines()


def resolve_is_open(processes: Sequence[str]) -> bool:
    """True when a running process's executable is inside the Resolve app bundle."""
    return any(RESOLVE_BUNDLE in process for process in processes)


def machine_serial() -> str:
    """The bay's hardware serial number (`IOPlatformSerialNumber`). Raises `RuntimeError` when it cannot be read."""
    try:
        done = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError(f"cannot read the serial number: {error}") from error
    found = re.search(r'"IOPlatformSerialNumber"\s*=\s*"([^"]+)"', done.stdout)
    if not found:
        raise RuntimeError("cannot read the serial number: ioreg printed none")
    return found.group(1)


def resolve_version(info_plist: Path = RESOLVE_INFO_PLIST) -> Optional[str]:
    """The installed Resolve version, or None when the app is not where it is expected."""
    try:
        with info_plist.open("rb") as handle:
            value = plistlib.load(handle).get("CFBundleShortVersionString")
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    return value if isinstance(value, str) else None


def build_report(home: Path, serial: str, *, processes: Sequence[str], version: Optional[str], now: datetime) -> dict[str, Any]:
    """The report for one bay. With Resolve open it scans nothing and says so."""
    report: dict[str, Any] = {
        "format_version": FORMAT_VERSION, "serial": serial, "scanner_version": SCANNER_VERSION,
        "scanned_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "resolve_version": version,
        "status": "ok", "local_copies": [], "errors": [],
    }
    if resolve_is_open(processes):
        report["status"] = "skipped_resolve_open"
    else:
        report["local_copies"], report["errors"] = scan_library(home)
    return report


def main(argv: Optional[Sequence[str]] = None, *, serial: Callable[[], str] = machine_serial,
         processes: Callable[[], list[str]] = running_processes, version: Callable[[], Optional[str]] = resolve_version,
         now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> int:
    parser = argparse.ArgumentParser(description="Scan this bay's local Resolve disk library and print one report as JSON.")
    parser.add_argument("--home", type=Path, default=Path.home(), help="the login user's home folder (default: this user's)")
    args = parser.parse_args(argv)
    try:
        report = build_report(args.home, serial(), processes=processes(), version=version(), now=now())
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f"bay_scanner: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
