"""One photo per movement, kept as a file with a pointer in the database (t6-core #669).

The files sit in their own folder (`T6_DRIVE_PHOTO_DIR`, a separate volume), never in the database.
Nothing here reads a photo into `t6.llm`: a photo uploaded to a movement is only stored and shown.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
import sqlite3
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from t6.registry.sqlite import SqliteProjectRegistry

PHOTO_VARIABLE = "T6_DRIVE_PHOTO_DIR"
LIMIT = 10 * 1024 * 1024  # bytes, after decoding
EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


class PhotoError(ValueError):
    """A refusal with the rule that names it."""

    def __init__(self, rule: str, reason: str) -> None:
        super().__init__(reason)
        self.rule, self.reason = rule, reason


def photo_dir(environ: Mapping[str, str] | None = None) -> Path | None:
    value = (os.environ if environ is None else environ).get(PHOTO_VARIABLE, "").strip()
    return Path(value) if value else None


def _looks_like(media_type: str, data: bytes) -> bool:
    if media_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if media_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def decode(photo: str, media_type: str) -> bytes:
    if media_type not in EXTENSIONS:
        raise PhotoError("media_type", "A photo must be a JPEG, PNG or WebP image")
    try:
        data = base64.b64decode(photo, validate=True)
    except (ValueError, binascii.Error):
        raise PhotoError("input", "The photo is not valid base64") from None
    if not data:
        raise PhotoError("input", "The photo is empty")
    if len(data) > LIMIT:
        raise PhotoError("too_large", f"A photo may be at most {LIMIT // (1024 * 1024)} MB")
    if not _looks_like(media_type, data):
        raise PhotoError("media_type", "The file is not the image type it says")
    return data


def _movement_exists(db: sqlite3.Connection, movement: str) -> bool:
    return db.execute("SELECT 1 FROM drive_movements WHERE id=?", (movement,)).fetchone() is not None


def store_file(folder: Path, key: str, data: bytes, media_type: str) -> tuple[str, str]:
    """Atomically store photo bytes in the shared movement/reference photo folder."""
    digest = hashlib.sha256(data).hexdigest()
    relative = f"{key[-2:]}/{key}-{digest[:12]}.{EXTENSIONS[media_type]}"
    final = folder / relative
    final.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=final.parent, suffix=".tmp", delete=False) as tmp:
        tmp.write(data)
    temporary = Path(tmp.name)
    try:
        os.chmod(temporary, 0o600)
        temporary.replace(final)
    finally:
        temporary.unlink(missing_ok=True)
    return relative, digest


def add_photo(registry: SqliteProjectRegistry, movement: str, data: bytes, media_type: str, *, actor: str,
              folder: Path | None) -> dict[str, Any]:
    """Store or replace the photo of a movement. The old file is removed after the new one is saved."""
    if folder is None:
        raise PhotoError("config", f"Set {PHOTO_VARIABLE}")
    old: str | None = None
    with registry._transaction() as db:
        if not _movement_exists(db, movement):
            raise PhotoError("movement", "Movement not found")
        relative, digest = store_file(folder, movement, data, media_type)
        row = db.execute("SELECT path FROM drive_movement_photos WHERE movement_id=?", (movement,)).fetchone()
        old = row[0] if row else None
        db.execute("INSERT OR REPLACE INTO drive_movement_photos (movement_id, path, media_type, size, sha256, added_by, at) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?)", (movement, relative, media_type, len(data), digest, actor, registry._clock()))
    if old and old != relative:
        (folder / old).unlink(missing_ok=True)
    return {"movement": movement, "media_type": media_type, "size": len(data)}


def read_photo(registry: SqliteProjectRegistry, movement: str, *, folder: Path | None) -> dict[str, Any]:
    if folder is None:
        raise PhotoError("config", f"Set {PHOTO_VARIABLE}")
    with registry._transaction() as db:
        row = db.execute("SELECT path, media_type, size, sha256 FROM drive_movement_photos WHERE movement_id=?", (movement,)).fetchone()
    if row is None:
        raise PhotoError("photo", "This movement has no photo")
    try:
        data = (folder / row[0]).read_bytes()
    except OSError:
        raise PhotoError("photo", "The photo file is missing") from None
    if hashlib.sha256(data).hexdigest() != row[3]:
        raise PhotoError("photo", "The photo file does not match its record")
    return {"movement": movement, "media_type": row[1], "size": row[2], "photo": base64.b64encode(data).decode()}


def delete_photo(registry: SqliteProjectRegistry, movement: str, *, folder: Path | None) -> dict[str, Any]:
    if folder is None:
        raise PhotoError("config", f"Set {PHOTO_VARIABLE}")
    with registry._transaction() as db:
        row = db.execute("SELECT path FROM drive_movement_photos WHERE movement_id=?", (movement,)).fetchone()
        if row is None:
            raise PhotoError("photo", "This movement has no photo")
        db.execute("DELETE FROM drive_movement_photos WHERE movement_id=?", (movement,))
    (folder / row[0]).unlink(missing_ok=True)
    return {"movement": movement, "deleted": True}


def forget_all(db: sqlite3.Connection) -> list[str]:
    """Drop every photo record (a practice reload replaces the movements) and return the files to remove after the commit."""
    paths = [row[0] for row in db.execute("SELECT path FROM drive_movement_photos")]
    db.execute("DELETE FROM drive_movement_photos")
    return paths


def remove_files(paths: list[str], folder: Path | None) -> None:
    if folder is None:
        return
    for relative in paths:
        (folder / relative).unlink(missing_ok=True)


def flag_rows(db: sqlite3.Connection, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark each movement row `photo`: 'upload', 'capture' (its TRANSMITTAL post kept a photo) or none."""
    uploaded = {row[0] for row in db.execute("SELECT movement_id FROM drive_movement_photos")}
    captured = {row[0] for row in db.execute("SELECT id FROM drive_captures WHERE length(photo) > 0")}
    for row in rows:
        source = "upload" if row["id"] in uploaded else "capture" if row.get("capture") in captured else None
        if source:
            row["photo"] = source  # a movement without a photo keeps the shape it always had
    return rows
