"""Backup, restore and health of the VPS databases. Standard library only.

The server keeps three SQLite files: the Project registry (authoritative
identity, bindings, reservations, audit and sync intents), the durable queue
and the operator actions. `backup` copies each with SQLite's online backup, so
a running server is never stopped and every file is a consistent snapshot. The
manifest records checksums and counts; `restore` verifies them before it
writes anything and again after.

The files are snapshots of one moment each, not of one instant together. That
is safe because the registry is the authority: after a restore, every sync
intent that Notion has not confirmed is marked for dispatch again, and the
server queues it on start. Syncs show current state, so doing one twice or late
changes nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from pathlib import Path
import sqlite3
import time

__all__ = ["ROLES", "OpsError", "backup", "counts", "health", "restore"]

ROLES = ("registry", "queue", "operator")
MANIFEST = "manifest.json"

_COUNTS = {
    "registry": {
        "projects": "SELECT COUNT(*) FROM projects",
        "bindings": "SELECT COUNT(*) FROM bindings",
        "audit": "SELECT COUNT(*) FROM audit",
        "rotation_in": "SELECT COUNT(*) FROM projects WHERE rotation = 'in'",
        "pending_sync": "SELECT COUNT(*) FROM sync_intents WHERE done_at IS NULL",
        "review_items": "SELECT COUNT(*) FROM review_items WHERE resolved_at IS NULL",
    },
    "queue": {
        "pending_jobs": "SELECT COUNT(*) FROM jobs WHERE state IN ('pending', 'claimed')",
        "failed_jobs": "SELECT COUNT(*) FROM jobs WHERE state = 'failed'",
    },
    "operator": {
        "pending_actions": "SELECT COUNT(*) FROM actions WHERE state IN ('pending', 'claimed')",
        "actions": "SELECT COUNT(*) FROM actions",
    },
}


class OpsError(Exception):
    """A backup or restore that cannot be trusted. Nothing was changed."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def counts(role: str, path: Path) -> dict[str, int]:
    """Semantic counts of one database, for comparing before and after."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise OpsError(f"{role} database fails its integrity check")
        return {name: int(db.execute(sql).fetchone()[0]) for name, sql in _COUNTS[role].items()}
    except sqlite3.DatabaseError as problem:
        raise OpsError(f"{role} database cannot be read: {problem}") from problem
    finally:
        db.close()


def backup(sources: Mapping[str, Path], into: Path) -> dict[str, object]:
    """Snapshot each existing database into `into` (an empty or new directory)."""
    into.mkdir(parents=True, exist_ok=True)
    if any(into.iterdir()):
        raise OpsError(f"{into} is not empty; a backup never overwrites another")
    files: dict[str, object] = {}
    for role in ROLES:
        source = sources.get(role)
        if source is None or not source.is_file():
            if role == "registry":
                raise OpsError(f"no registry database at {source}")
            continue  # the operator database exists only once operator actions are on
        target = into / f"{role}.sqlite"
        reading = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
        writing = sqlite3.connect(target)
        try:
            reading.backup(writing)
        finally:
            writing.close()
            reading.close()
        files[role] = {"file": target.name, "sha256": _sha256(target), "counts": counts(role, target)}
    manifest: dict[str, object] = {"version": 1, "taken_at": time.time(), "files": files}
    (into / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def _verified(source: Path) -> dict[str, dict[str, object]]:
    try:
        manifest = json.loads((source / MANIFEST).read_text(encoding="utf-8"))
        files: dict[str, dict[str, object]] = manifest["files"]
    except (OSError, ValueError, KeyError) as problem:
        raise OpsError(f"{source} has no readable manifest") from problem
    for role, entry in files.items():
        path = source / str(entry["file"])
        if role not in ROLES or not path.is_file() or _sha256(path) != entry["sha256"]:
            raise OpsError(f"{role} backup does not match its checksum")
        if counts(role, path) != entry["counts"]:
            raise OpsError(f"{role} backup does not match its recorded counts")
    if "registry" not in files:
        raise OpsError("the backup holds no registry")
    return files


def restore(source: Path, targets: Mapping[str, Path]) -> dict[str, dict[str, int]]:
    """Put a verified backup in place. Refuses to overwrite any existing database.

    Returns the counts of what was restored, which equal the manifest's. Sync
    intents Notion has not confirmed are marked for dispatch again.
    """
    files = _verified(source)
    for role in files:
        if targets[role].exists():
            raise OpsError(f"{targets[role]} exists; move it away first, a restore never overwrites")
    restored: dict[str, dict[str, int]] = {}
    for role, entry in files.items():
        target = targets[role]
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".restoring")
        reading = sqlite3.connect(f"file:{source / str(entry['file'])}?mode=ro", uri=True)
        writing = sqlite3.connect(partial)
        try:
            reading.backup(writing)
            if role == "registry":
                writing.execute("UPDATE sync_intents SET dispatched_at = NULL WHERE done_at IS NULL")
                writing.commit()
        finally:
            writing.close()
            reading.close()
        partial.replace(target)
        restored[role] = counts(role, target)
        if restored[role] != entry["counts"]:
            raise OpsError(f"{role} restored with different counts than the backup")
    return restored


def health(paths: Mapping[str, Path], now: float | None = None) -> dict[str, object]:
    """What an operator checks: sync lag, persistent failures, stuck work. Reads only."""
    now = time.time() if now is None else now
    report: dict[str, object] = {"ok": True, "problems": []}
    problems: list[str] = []
    for role in ROLES:
        path = paths.get(role)
        if path is None or not path.is_file():
            if role != "operator":
                problems.append(f"no {role} database at {path}")
            continue
        report[role] = counts(role, path)
    registry = paths.get("registry")
    if registry is not None and registry.is_file():
        db = sqlite3.connect(f"file:{registry}?mode=ro", uri=True, timeout=30)
        try:
            oldest = db.execute("SELECT MIN(created_at) FROM sync_intents WHERE done_at IS NULL").fetchone()[0]
            failed = db.execute(
                "SELECT COUNT(*) FROM sync_intents WHERE done_at IS NULL AND error_final = 1"
            ).fetchone()[0]
        finally:
            db.close()
        report["sync_lag_seconds"] = None if oldest is None else round(max(0.0, now - oldest), 1)
        report["sync_persistent_failures"] = int(failed)
        if failed:
            problems.append(f"{failed} Notion sync(s) failed after retries; the changes stand in the registry")
    failed_jobs = report.get("queue", {})
    if isinstance(failed_jobs, dict) and failed_jobs.get("failed_jobs"):
        problems.append(f"{failed_jobs['failed_jobs']} queue job(s) failed after their attempts")
    report["problems"] = problems
    report["ok"] = not problems
    return report
