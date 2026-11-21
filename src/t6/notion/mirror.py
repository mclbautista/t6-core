"""Local SQLite WAL mirror for Notion data sources and pages."""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from t6.notion.client import RequestPacer
from t6.registry.core import normalize_page_id

if TYPE_CHECKING:
    from t6.notion.source import NotionSource
    from t6.notion.sync import NotionClient

JSON = dict[str, Any]

MIRROR_ENABLED_ENV = "T6_NOTION_MIRROR"
MIRROR_DB_ENV = "T6_NOTION_MIRROR_DB"
DEFAULT_MAX_AGE_SECONDS = 300.0
DEFAULT_POLL_SECONDS = 60.0
DEFAULT_RESCAN_SECONDS = 1800.0
OVERLAP_SECONDS = 60.0
MIN_REQUEST_INTERVAL_SECONDS = 0.4

__all__ = [
    "DEFAULT_MAX_AGE_SECONDS",
    "DEFAULT_POLL_SECONDS",
    "DEFAULT_RESCAN_SECONDS",
    "MIN_REQUEST_INTERVAL_SECONDS",
    "MIRROR_DB_ENV",
    "MIRROR_ENABLED_ENV",
    "OVERLAP_SECONDS",
    "MirrorReader",
    "MirrorSourceState",
    "MirrorSyncer",
    "MirrorWriter",
    "RequestPacer",
    "mirror_db_path",
    "mirror_enabled",
]

_TRUTHY = frozenset({"1", "true", "on", "yes"})
_FALSY = frozenset({"0", "false", "off", "no"})

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS mirror_pages (
    data_source_id TEXT NOT NULL,
    page_id TEXT NOT NULL,
    last_edited_time TEXT NOT NULL DEFAULT '',
    archived INTEGER NOT NULL DEFAULT 0,
    deleted INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL,
    synced_at REAL NOT NULL,
    PRIMARY KEY (data_source_id, page_id)
);
CREATE INDEX IF NOT EXISTS mirror_pages_by_page ON mirror_pages (page_id);
CREATE INDEX IF NOT EXISTS mirror_pages_active ON mirror_pages (data_source_id, archived, deleted);

CREATE TABLE IF NOT EXISTS mirror_state (
    data_source_id TEXT PRIMARY KEY,
    source_key TEXT NOT NULL DEFAULT '',
    high_water_mark TEXT,
    last_sync_at REAL,
    last_full_rescan_at REAL,
    row_count INTEGER NOT NULL DEFAULT 0,
    ready INTEGER NOT NULL DEFAULT 0,
    max_age_seconds REAL,
    source_json TEXT
);
"""


def mirror_enabled(environ: Mapping[str, str]) -> bool:
    """Return whether the local Notion mirror is enabled (default True unless explicitly turned off)."""
    raw = environ.get(MIRROR_ENABLED_ENV)
    if raw is None or not raw.strip():
        return True
    normalized = raw.strip().lower()
    if normalized in _TRUTHY:
        return True
    if normalized in _FALSY:
        return False
    raise ValueError(f"invalid {MIRROR_ENABLED_ENV} value: {raw!r}")


def mirror_db_path(environ: Mapping[str, str]) -> Path | None:
    """Return the configured mirror SQLite path, or None when disabled or unset."""
    if not mirror_enabled(environ):
        return None
    raw = (environ.get(MIRROR_DB_ENV) or "").strip()
    return Path(raw) if raw else None


@dataclass(frozen=True, slots=True)
class MirrorSourceState:
    """Stored sync state for one mirrored Notion data source."""

    data_source_id: str
    source_key: str
    high_water_mark: str | None
    last_sync_at: float | None
    last_full_rescan_at: float | None
    row_count: int
    ready: bool
    max_age_seconds: float | None = None
    source_json: str | None = None


def _parse_iso(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_iso_z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _max_iso(current: str | None, candidate: str | None) -> str | None:
    if not candidate:
        return current
    if not current:
        return candidate
    cur_dt, cand_dt = _parse_iso(current), _parse_iso(candidate)
    if cur_dt is not None and cand_dt is not None:
        return candidate if cand_dt >= cur_dt else current
    return candidate if candidate >= current else current


def _is_archived(page: Mapping[str, Any]) -> bool:
    return bool(page.get("archived") or page.get("in_trash"))


class MirrorWriter:
    """Owner-only (`0o600`) SQLite WAL writer for mirrored Notion data sources and pages."""

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self._path = Path(path)
        self._clock = clock
        self._ensure_ready()

    @property
    def path(self) -> Path:
        return self._path

    def _ensure_permissions(self) -> None:
        for candidate in (self._path, Path(f"{self._path}-wal"), Path(f"{self._path}-shm")):
            if candidate.exists():
                os.chmod(candidate, 0o600)

    def _create_empty_file(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self._path, 0o600)

    def _rebuild(self) -> None:
        for candidate in (self._path, Path(f"{self._path}-wal"), Path(f"{self._path}-shm")):
            candidate.unlink(missing_ok=True)
        self._create_empty_file()
        conn = sqlite3.connect(str(self._path), timeout=30, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA_SQL)
        finally:
            conn.close()
        self._ensure_permissions()

    def _open_verified(self) -> sqlite3.Connection:
        if not self._path.exists() or self._path.stat().st_size == 0:
            self._rebuild()
        for attempt in range(2):
            conn: sqlite3.Connection | None = None
            try:
                conn = sqlite3.connect(str(self._path), timeout=30, isolation_level=None)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                check = conn.execute("PRAGMA quick_check(1)").fetchone()
                if check is None or check[0] != "ok":
                    raise sqlite3.DatabaseError("mirror quick_check failed")
                tables = {
                    row[0]
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
                }
                if not {"mirror_pages", "mirror_state"}.issubset(tables):
                    raise sqlite3.DatabaseError("mirror schema missing")
                self._ensure_permissions()
                return conn
            except (sqlite3.DatabaseError, OSError):
                if conn is not None:
                    conn.close()
                if attempt == 1:
                    raise
                self._rebuild()
        raise RuntimeError("unreachable")

    def _ensure_ready(self) -> None:
        conn = self._open_verified()
        conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._open_verified()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()
        self._ensure_permissions()

    def _run_with_rebuild(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        for attempt in range(2):
            try:
                with self._transaction() as db:
                    return operation(db)
            except (sqlite3.DatabaseError, OSError):
                if attempt == 1:
                    raise
                self._rebuild()
        raise RuntimeError("unreachable")

    def state(self, data_source_id: str) -> MirrorSourceState | None:
        """Return stored sync metadata for `data_source_id`, or None if never synced."""
        ds_key = normalize_page_id(data_source_id)

        def read(db: sqlite3.Connection) -> MirrorSourceState | None:
            row = db.execute(
                "SELECT data_source_id, source_key, high_water_mark, last_sync_at, "
                "last_full_rescan_at, row_count, ready, max_age_seconds, source_json "
                "FROM mirror_state WHERE data_source_id = ?",
                (ds_key,),
            ).fetchone()
            if row is None:
                return None
            return MirrorSourceState(
                data_source_id=str(row[0]),
                source_key=str(row[1]),
                high_water_mark=str(row[2]) if row[2] else None,
                last_sync_at=float(row[3]) if row[3] is not None else None,
                last_full_rescan_at=float(row[4]) if row[4] is not None else None,
                row_count=int(row[5]),
                ready=bool(row[6]),
                max_age_seconds=float(row[7]) if row[7] is not None else None,
                source_json=str(row[8]) if row[8] is not None else None,
            )

        result: MirrorSourceState | None = self._run_with_rebuild(read)
        return result

    def upsert_pages(
        self,
        data_source_id: str,
        pages: Sequence[JSON],
        *,
        synced_at: float | None = None,
        source_key: str = "",
    ) -> int:
        """Upsert changed pages for `data_source_id` and update high-water mark and `last_sync_at`."""
        ds_key = normalize_page_id(data_source_id)
        ts = self._clock() if synced_at is None else synced_at

        def write(db: sqlite3.Connection) -> int:
            existing = db.execute(
                "SELECT source_key, high_water_mark, last_full_rescan_at, ready, max_age_seconds, source_json "
                "FROM mirror_state WHERE data_source_id = ?",
                (ds_key,),
            ).fetchone()
            hwm: str | None = str(existing[1]) if existing and existing[1] else None
            rows: list[tuple[str, str, str, int, int, str, float]] = []
            for page in pages:
                page_id = normalize_page_id(str(page["id"]))
                edited = str(page.get("last_edited_time") or "")
                hwm = _max_iso(hwm, edited)
                archived = 1 if _is_archived(page) else 0
                deleted = 1 if page.get("in_trash") else 0
                rows.append((ds_key, page_id, edited, archived, deleted, json.dumps(page, ensure_ascii=False), ts))
            if rows:
                db.executemany(
                    "INSERT INTO mirror_pages (data_source_id, page_id, last_edited_time, archived, deleted, raw_json, synced_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(data_source_id, page_id) DO UPDATE SET "
                    "last_edited_time = excluded.last_edited_time, "
                    "archived = excluded.archived, "
                    "deleted = excluded.deleted, "
                    "raw_json = excluded.raw_json, "
                    "synced_at = excluded.synced_at",
                    rows,
                )
            live_count = int(
                db.execute(
                    "SELECT COUNT(*) FROM mirror_pages WHERE data_source_id = ? AND deleted = 0 AND archived = 0",
                    (ds_key,),
                ).fetchone()[0]
            )
            key = source_key or (str(existing[0]) if existing else "")
            last_rescan = existing[2] if existing else None
            ready = int(existing[3]) if existing else 0
            max_age = existing[4] if existing else None
            source_json = existing[5] if existing else None
            db.execute(
                "INSERT INTO mirror_state (data_source_id, source_key, high_water_mark, last_sync_at, "
                "last_full_rescan_at, row_count, ready, max_age_seconds, source_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(data_source_id) DO UPDATE SET "
                "source_key = excluded.source_key, "
                "high_water_mark = excluded.high_water_mark, "
                "last_sync_at = excluded.last_sync_at, "
                "row_count = excluded.row_count",
                (ds_key, key, hwm, ts, last_rescan, live_count, ready, max_age, source_json),
            )
            return live_count

        count: int = self._run_with_rebuild(write)
        return count

    def replace_data_source(
        self,
        data_source_id: str,
        pages: Sequence[JSON],
        *,
        synced_at: float | None = None,
        source_key: str = "",
        source_meta: JSON | None = None,
        max_age_seconds: float | None = None,
    ) -> int:
        """Replace the active page set for `data_source_id`, marking omitted pages deleted and setting `ready=1`."""
        ds_key = normalize_page_id(data_source_id)
        ts = self._clock() if synced_at is None else synced_at

        def write(db: sqlite3.Connection) -> int:
            existing_state = db.execute(
                "SELECT source_key, max_age_seconds, source_json FROM mirror_state WHERE data_source_id = ?",
                (ds_key,),
            ).fetchone()
            seen_ids: set[str] = set()
            hwm: str | None = None
            rows: list[tuple[str, str, str, int, int, str, float]] = []
            expected_live = 0
            for page in pages:
                page_id = normalize_page_id(str(page["id"]))
                seen_ids.add(page_id)
                edited = str(page.get("last_edited_time") or "")
                hwm = _max_iso(hwm, edited)
                archived = 1 if _is_archived(page) else 0
                deleted = 1 if page.get("in_trash") else 0
                if not archived and not deleted:
                    expected_live += 1
                rows.append((ds_key, page_id, edited, archived, deleted, json.dumps(page, ensure_ascii=False), ts))
            if rows:
                db.executemany(
                    "INSERT INTO mirror_pages (data_source_id, page_id, last_edited_time, archived, deleted, raw_json, synced_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(data_source_id, page_id) DO UPDATE SET "
                    "last_edited_time = excluded.last_edited_time, "
                    "archived = excluded.archived, "
                    "deleted = excluded.deleted, "
                    "raw_json = excluded.raw_json, "
                    "synced_at = excluded.synced_at",
                    rows,
                )
            existing_ids = {
                str(r[0])
                for r in db.execute(
                    "SELECT page_id FROM mirror_pages WHERE data_source_id = ? AND deleted = 0",
                    (ds_key,),
                ).fetchall()
            }
            omitted = existing_ids - seen_ids
            if omitted:
                db.executemany(
                    "UPDATE mirror_pages SET deleted = 1, synced_at = ? WHERE data_source_id = ? AND page_id = ?",
                    [(ts, ds_key, pid) for pid in omitted],
                )
            db_live = int(
                db.execute(
                    "SELECT COUNT(*) FROM mirror_pages WHERE data_source_id = ? AND deleted = 0 AND archived = 0",
                    (ds_key,),
                ).fetchone()[0]
            )
            if db_live != expected_live:
                raise RuntimeError(f"mirror rescan row_count mismatch: db={db_live} expected={expected_live}")
            key = source_key or (str(existing_state[0]) if existing_state else "")
            stored_max_age = max_age_seconds if max_age_seconds is not None else (existing_state[1] if existing_state else None)
            stored_source_json = (
                json.dumps(source_meta, ensure_ascii=False)
                if source_meta is not None
                else (existing_state[2] if existing_state else None)
            )
            db.execute(
                "INSERT INTO mirror_state (data_source_id, source_key, high_water_mark, last_sync_at, "
                "last_full_rescan_at, row_count, ready, max_age_seconds, source_json) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?) "
                "ON CONFLICT(data_source_id) DO UPDATE SET "
                "source_key = excluded.source_key, "
                "high_water_mark = excluded.high_water_mark, "
                "last_sync_at = excluded.last_sync_at, "
                "last_full_rescan_at = excluded.last_full_rescan_at, "
                "row_count = excluded.row_count, "
                "ready = 1, "
                "max_age_seconds = excluded.max_age_seconds, "
                "source_json = excluded.source_json",
                (ds_key, key, hwm, ts, ts, db_live, stored_max_age, stored_source_json),
            )
            return db_live

        count: int = self._run_with_rebuild(write)
        return count

    def upsert_page(
        self,
        page: Mapping[str, Any],
        *,
        active_data_source_ids: Collection[str] | None = None,
        synced_at: float | None = None,
        source_key: str = "",
    ) -> str:
        """Upsert a single mirrored page if it belongs to an active data source and passes monotonic guard."""
        parent = page.get("parent")
        raw_ds = ""
        if isinstance(parent, Mapping):
            raw_ds = str(parent.get("data_source_id") or parent.get("database_id") or "")
        ds_key = normalize_page_id(raw_ds)
        if not ds_key:
            return "not_mirrored"
        if active_data_source_ids is not None:
            allowed = {normalize_page_id(ds) for ds in active_data_source_ids if ds}
            if ds_key not in allowed:
                return "not_mirrored"
        page_id = normalize_page_id(str(page.get("id") or ""))
        if not page_id:
            return "ignored"
        edited = str(page.get("last_edited_time") or "")
        new_ts = _parse_iso(edited)
        ts = self._clock() if synced_at is None else synced_at

        def write(db: sqlite3.Connection) -> str:
            existing_page = db.execute(
                "SELECT last_edited_time FROM mirror_pages WHERE data_source_id = ? AND page_id = ?",
                (ds_key, page_id),
            ).fetchone()
            if existing_page is not None:
                stored_ts = _parse_iso(str(existing_page[0]) if existing_page[0] is not None else None)
                if stored_ts is not None and new_ts is not None and new_ts < stored_ts:
                    return "stale_ignored"
            archived = 1 if page.get("archived") else 0
            deleted = 1 if (page.get("in_trash") or page.get("archived")) else 0
            db.execute(
                "INSERT INTO mirror_pages (data_source_id, page_id, last_edited_time, archived, deleted, raw_json, synced_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(data_source_id, page_id) DO UPDATE SET "
                "last_edited_time = excluded.last_edited_time, "
                "archived = excluded.archived, "
                "deleted = excluded.deleted, "
                "raw_json = excluded.raw_json, "
                "synced_at = excluded.synced_at",
                (ds_key, page_id, edited, archived, deleted, json.dumps(dict(page), ensure_ascii=False), ts),
            )
            live_count = int(
                db.execute(
                    "SELECT COUNT(*) FROM mirror_pages WHERE data_source_id = ? AND deleted = 0 AND archived = 0",
                    (ds_key,),
                ).fetchone()[0]
            )
            existing_state = db.execute(
                "SELECT source_key, high_water_mark, last_full_rescan_at, ready, max_age_seconds, source_json "
                "FROM mirror_state WHERE data_source_id = ?",
                (ds_key,),
            ).fetchone()
            hwm = _max_iso(str(existing_state[1]) if existing_state and existing_state[1] else None, edited)
            key = source_key or (str(existing_state[0]) if existing_state else "")
            last_rescan = existing_state[2] if existing_state else None
            ready = int(existing_state[3]) if existing_state else 0
            max_age = existing_state[4] if existing_state else None
            source_json = existing_state[5] if existing_state else None
            db.execute(
                "INSERT INTO mirror_state (data_source_id, source_key, high_water_mark, last_sync_at, "
                "last_full_rescan_at, row_count, ready, max_age_seconds, source_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(data_source_id) DO UPDATE SET "
                "source_key = excluded.source_key, "
                "high_water_mark = excluded.high_water_mark, "
                "last_sync_at = excluded.last_sync_at, "
                "row_count = excluded.row_count",
                (ds_key, key, hwm, ts, last_rescan, live_count, ready, max_age, source_json),
            )
            return "upserted"

        outcome: str = self._run_with_rebuild(write)
        return outcome

    def mark_page_deleted(
        self,
        page_id: str,
        *,
        timestamp: str | None = None,
        data_source_id: str | None = None,
        active_data_source_ids: Collection[str] | None = None,
        synced_at: float | None = None,
    ) -> str:
        """Mark existing mirrored rows for `page_id` as deleted unless a newer `last_edited_time` is stored."""
        p_key = normalize_page_id(page_id)
        if not p_key:
            return "not_mirrored"
        ds_filter = normalize_page_id(data_source_id) if data_source_id else None
        allowed = (
            {normalize_page_id(ds) for ds in active_data_source_ids if ds}
            if active_data_source_ids is not None
            else None
        )
        event_ts = _parse_iso(timestamp)
        ts = self._clock() if synced_at is None else synced_at

        def write(db: sqlite3.Connection) -> str:
            if ds_filter:
                rows = db.execute(
                    "SELECT data_source_id, last_edited_time FROM mirror_pages WHERE data_source_id = ? AND page_id = ?",
                    (ds_filter, p_key),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT data_source_id, last_edited_time FROM mirror_pages WHERE page_id = ?",
                    (p_key,),
                ).fetchall()
            if allowed is not None:
                rows = [row for row in rows if str(row[0]) in allowed]
            if not rows:
                return "not_mirrored"
            updated = 0
            for row in rows:
                ds_key = str(row[0])
                stored_raw = str(row[1] or "")
                stored_ts = _parse_iso(stored_raw)
                if event_ts is not None and stored_ts is not None and event_ts < stored_ts:
                    continue
                new_edited = stored_raw
                if timestamp and event_ts is not None and (stored_ts is None or event_ts > stored_ts):
                    new_edited = timestamp
                db.execute(
                    "UPDATE mirror_pages SET deleted = 1, last_edited_time = ?, synced_at = ? "
                    "WHERE data_source_id = ? AND page_id = ?",
                    (new_edited, ts, ds_key, p_key),
                )
                live_count = int(
                    db.execute(
                        "SELECT COUNT(*) FROM mirror_pages WHERE data_source_id = ? AND deleted = 0 AND archived = 0",
                        (ds_key,),
                    ).fetchone()[0]
                )
                existing_state = db.execute(
                    "SELECT high_water_mark FROM mirror_state WHERE data_source_id = ?",
                    (ds_key,),
                ).fetchone()
                if existing_state is not None:
                    hwm = _max_iso(str(existing_state[0]) if existing_state[0] else None, new_edited or None)
                    db.execute(
                        "UPDATE mirror_state SET high_water_mark = ?, last_sync_at = ?, row_count = ? "
                        "WHERE data_source_id = ?",
                        (hwm, ts, live_count, ds_key),
                    )
                updated += 1
            return "deleted" if updated > 0 else "stale_ignored"

        outcome: str = self._run_with_rebuild(write)
        return outcome

    def handle_page_changed(
        self,
        notion: NotionSource,
        payload: Mapping[str, Any],
        *,
        active_data_source_ids: Collection[str] | None = None,
    ) -> str:
        """Apply a `notion.page_changed` webhook/reconcile payload to the mirror."""
        from t6.notion.errors import NotionError

        raw_page_id = payload.get("page_id")
        if not raw_page_id and isinstance(payload.get("entity"), Mapping):
            raw_page_id = payload["entity"].get("id")
        page_id = str(raw_page_id).strip() if raw_page_id is not None else ""
        if not page_id:
            return "ignored"
        raw_event_type = payload.get("event_type") or payload.get("type")
        event_type = (
            str(raw_event_type).strip()
            if isinstance(raw_event_type, str) and raw_event_type.strip()
            else None
        )
        raw_ts = payload.get("timestamp")
        event_ts = str(raw_ts).strip() if isinstance(raw_ts, str) and raw_ts.strip() else None

        if event_type == "page.deleted":
            return self.mark_page_deleted(
                page_id,
                timestamp=event_ts,
                active_data_source_ids=active_data_source_ids,
            )
        try:
            page = notion.retrieve_page(page_id, freshness="live")
        except NotionError as error:
            if error.status == 404:
                return self.mark_page_deleted(
                    page_id,
                    timestamp=event_ts,
                    active_data_source_ids=active_data_source_ids,
                )
            raise
        return self.upsert_page(page, active_data_source_ids=active_data_source_ids)

    def refresh_page(
        self,
        notion: NotionSource,
        payload: Mapping[str, Any],
        *,
        active_data_source_ids: Collection[str] | None = None,
    ) -> str:
        """Fetch `page_id` live after an API verb Notion write and update or mark deleted in mirror."""
        from t6.notion.errors import NotionError

        raw_page_id = payload.get("page_id")
        page_id = str(raw_page_id).strip() if raw_page_id is not None else ""
        if not page_id:
            return "ignored"
        try:
            page = notion.retrieve_page(page_id, freshness="live")
        except NotionError as error:
            if error.status == 404:
                return self.mark_page_deleted(
                    page_id,
                    active_data_source_ids=active_data_source_ids,
                )
            raise
        raw_key = payload.get("data_source_key")
        source_key = str(raw_key) if isinstance(raw_key, str) else ""
        return self.upsert_page(
            page,
            active_data_source_ids=active_data_source_ids,
            source_key=source_key,
        )

    def close(self) -> None:
        """No persistent connection is kept between operations."""


def _matches_timestamp(raw_value: object, condition: Mapping[str, Any]) -> bool:
    edited = _parse_iso(raw_value if isinstance(raw_value, str) else None)
    if edited is None:
        return False
    checks: tuple[tuple[str, Callable[[datetime, datetime], bool]], ...] = (
        ("on_or_after", lambda a, b: a >= b),
        ("after", lambda a, b: a > b),
        ("on_or_before", lambda a, b: a <= b),
        ("before", lambda a, b: a < b),
        ("equals", lambda a, b: a == b),
    )
    for op, compare in checks:
        if op in condition:
            bound = _parse_iso(condition[op] if isinstance(condition[op], str) else None)
            if bound is None or not compare(edited, bound):
                return False
    return True


def _find_property(page: Mapping[str, Any], prop_key: str) -> Mapping[str, Any]:
    props = page.get("properties")
    if not isinstance(props, Mapping):
        return {}
    direct = props.get(prop_key)
    if isinstance(direct, Mapping):
        return direct
    wanted = unquote(prop_key)
    for prop in props.values():
        if isinstance(prop, Mapping) and unquote(str(prop.get("id", ""))) == wanted:
            return prop
    return {}


def _matches_filter(page: Mapping[str, Any], query_filter: Mapping[str, Any] | None) -> bool:
    if not query_filter:
        return True
    if "and" in query_filter:
        parts = query_filter["and"]
        return isinstance(parts, Sequence) and all(
            isinstance(part, Mapping) and _matches_filter(page, part) for part in parts
        )
    if "or" in query_filter:
        parts = query_filter["or"]
        return isinstance(parts, Sequence) and any(
            isinstance(part, Mapping) and _matches_filter(page, part) for part in parts
        )
    ts_kind = query_filter.get("timestamp")
    if ts_kind in ("last_edited_time", "created_time") and isinstance(query_filter.get(ts_kind), Mapping):
        return _matches_timestamp(page.get(ts_kind), query_filter[ts_kind])
    prop_key = query_filter.get("property")
    if not isinstance(prop_key, str):
        return True
    prop = _find_property(page, prop_key)
    if "relation" in query_filter and isinstance(query_filter["relation"], Mapping):
        rel_cond = query_filter["relation"]
        items = prop.get("relation") or ()
        rel_ids = {
            normalize_page_id(str(item.get("id") or ""))
            for item in items
            if isinstance(item, Mapping) and item.get("id")
        }
        if "contains" in rel_cond and normalize_page_id(str(rel_cond["contains"])) not in rel_ids:
            return False
        if "does_not_contain" in rel_cond and normalize_page_id(str(rel_cond["does_not_contain"])) in rel_ids:
            return False
        if "is_empty" in rel_cond and (len(items) == 0) != bool(rel_cond["is_empty"]):
            return False
        if "is_not_empty" in rel_cond and (len(items) > 0) != bool(rel_cond["is_not_empty"]):
            return False
        return True
    if "multi_select" in query_filter and isinstance(query_filter["multi_select"], Mapping):
        ms_cond = query_filter["multi_select"]
        raw_items = prop.get("multi_select") or ()
        names = [
            str(item.get("name"))
            for item in raw_items
            if isinstance(item, Mapping) and item.get("name") is not None
        ]
        if "contains" in ms_cond and str(ms_cond["contains"]) not in names:
            return False
        if "does_not_contain" in ms_cond and str(ms_cond["does_not_contain"]) in names:
            return False
        if "is_empty" in ms_cond and (len(names) == 0) != bool(ms_cond["is_empty"]):
            return False
        if "is_not_empty" in ms_cond and (len(names) > 0) != bool(ms_cond["is_not_empty"]):
            return False
        return True
    for kind in ("select", "status", "rich_text", "title", "number", "checkbox"):
        kind_cond = query_filter.get(kind)
        if not isinstance(kind_cond, Mapping):
            continue
        raw_val = prop.get(kind)
        value: Any = raw_val
        if kind in ("select", "status"):
            value = raw_val.get("name") if isinstance(raw_val, Mapping) else None
        elif kind in ("rich_text", "title"):
            value = "".join(
                str(part.get("plain_text", "")) for part in (raw_val or ()) if isinstance(part, Mapping)
            )
        if "equals" in kind_cond and value != kind_cond["equals"]:
            return False
        if "does_not_equal" in kind_cond and value == kind_cond["does_not_equal"]:
            return False
        if kind in ("rich_text", "title"):
            text = str(value or "")
            if "contains" in kind_cond and str(kind_cond["contains"]) not in text:
                return False
            if "does_not_contain" in kind_cond and str(kind_cond["does_not_contain"]) in text:
                return False
            if "starts_with" in kind_cond and not text.startswith(str(kind_cond["starts_with"])):
                return False
            if "ends_with" in kind_cond and not text.endswith(str(kind_cond["ends_with"])):
                return False
        if kind == "number":
            number_preds: tuple[tuple[str, Callable[[float, float], bool]], ...] = (
                ("greater_than", lambda a, b: a > b),
                ("less_than", lambda a, b: a < b),
                ("greater_than_or_equal_to", lambda a, b: a >= b),
                ("less_than_or_equal_to", lambda a, b: a <= b),
            )
            for op, pred in number_preds:
                if op in kind_cond:
                    if not isinstance(value, (int, float)) or isinstance(value, bool):
                        return False
                    bound = kind_cond[op]
                    if (
                        not isinstance(bound, (int, float))
                        or isinstance(bound, bool)
                        or not pred(float(value), float(bound))
                    ):
                        return False
        empty = value is None or value == ""
        if "is_empty" in kind_cond and empty != bool(kind_cond["is_empty"]):
            return False
        if "is_not_empty" in kind_cond and (not empty) != bool(kind_cond["is_not_empty"]):
            return False
        return True
    if "date" in query_filter and isinstance(query_filter["date"], Mapping):
        date_cond = query_filter["date"]
        date_val = prop.get("date")
        start = date_val.get("start") if isinstance(date_val, Mapping) else None
        has_start = isinstance(start, str) and bool(start.strip())
        if "is_empty" in date_cond and (not has_start) != bool(date_cond["is_empty"]):
            return False
        if "is_not_empty" in date_cond and has_start != bool(date_cond["is_not_empty"]):
            return False
        if any(op in date_cond for op in ("equals", "before", "after", "on_or_before", "on_or_after")):
            if not has_start or not _matches_timestamp(start, date_cond):
                return False
        return True
    return True


def _sort_pages(pages: list[JSON], sorts: Sequence[Mapping[str, Any]] | None) -> list[JSON] | None:
    if not sorts:
        return pages
    for spec in reversed(sorts):
        if not isinstance(spec, Mapping):
            return None
        field = spec.get("timestamp")
        if field not in ("created_time", "last_edited_time"):
            return None
        direction = str(spec.get("direction", "ascending")).lower()
        if direction not in ("ascending", "descending"):
            return None
        reverse = direction == "descending"
        sentinel = float("-inf") if reverse else float("inf")

        def sort_key(page: JSON, *, _field: str = str(field), _sentinel: float = sentinel) -> float:
            raw = page.get(_field)
            dt = _parse_iso(raw if isinstance(raw, str) else None)
            return dt.timestamp() if dt is not None else _sentinel

        pages.sort(key=sort_key, reverse=reverse)
    return pages


class MirrorReader:
    """Read-only (`file:<path>?mode=ro`) reader for mirrored Notion data sources and pages."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_age_seconds: float | Mapping[str, float] = DEFAULT_MAX_AGE_SECONDS,
        clock: Callable[[], float] = time.time,
        enabled_data_sources: Collection[str] | None = None,
    ) -> None:
        self._path = Path(path)
        if isinstance(max_age_seconds, Mapping):
            self._default_max_age = DEFAULT_MAX_AGE_SECONDS
            self._max_ages = {normalize_page_id(k): float(v) for k, v in max_age_seconds.items()}
        else:
            self._default_max_age = float(max_age_seconds)
            self._max_ages = {}
        self._clock = clock
        self._enabled = (
            None
            if enabled_data_sources is None
            else frozenset(normalize_page_id(ds) for ds in enabled_data_sources)
        )

    def _is_enabled_ds(self, ds_key: str) -> bool:
        return self._enabled is None or ds_key in self._enabled

    def _max_age_for(self, ds_key: str, stored_max_age: float | None = None) -> float:
        if ds_key in self._max_ages:
            return self._max_ages[ds_key]
        if stored_max_age is not None and stored_max_age > 0:
            return float(stored_max_age)
        return self._default_max_age

    def _open_ro(self) -> sqlite3.Connection | None:
        if not self._path.is_file() or self._path.stat().st_size == 0:
            return None
        uri = f"{self._path.resolve().as_uri()}?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)

    def _is_fresh(self, db: sqlite3.Connection, ds_key: str, now: float) -> bool:
        row = db.execute(
            "SELECT ready, last_sync_at, max_age_seconds FROM mirror_state WHERE data_source_id = ?",
            (ds_key,),
        ).fetchone()
        if row is None or not row[0] or row[1] is None:
            return False
        max_age = self._max_age_for(ds_key, float(row[2]) if row[2] is not None else None)
        return (now - float(row[1])) <= max_age

    def query_data_source(
        self,
        data_source_id: str,
        *,
        filter: JSON | None = None,
        sorts: list[JSON] | None = None,
        page_size: int = 100,
    ) -> list[JSON] | None:
        """Return live mirrored pages for `data_source_id`, or None when unavailable/not ready/stale."""
        _ = page_size
        ds_key = normalize_page_id(data_source_id)
        if not self._is_enabled_ds(ds_key):
            return None
        conn: sqlite3.Connection | None = None
        try:
            conn = self._open_ro()
            if conn is None:
                return None
            if not self._is_fresh(conn, ds_key, self._clock()):
                return None
            rows = conn.execute(
                "SELECT raw_json FROM mirror_pages "
                "WHERE data_source_id = ? AND deleted = 0 AND archived = 0 ORDER BY rowid",
                (ds_key,),
            ).fetchall()
            pages = [json.loads(str(r[0])) for r in rows]
            matched = [page for page in pages if _matches_filter(page, filter)]
            return _sort_pages(matched, sorts)
        except (sqlite3.Error, OSError, json.JSONDecodeError, ValueError):
            return None
        finally:
            if conn is not None:
                conn.close()

    def retrieve_page(self, page_id: str) -> JSON | None:
        """Return mirrored page JSON for `page_id`, or None if missing/deleted/archived/stale/unavailable."""
        p_key = normalize_page_id(page_id)
        if not p_key:
            return None
        conn: sqlite3.Connection | None = None
        try:
            conn = self._open_ro()
            if conn is None:
                return None
            row = conn.execute(
                "SELECT data_source_id, raw_json, deleted, archived FROM mirror_pages "
                "WHERE page_id = ? ORDER BY deleted ASC, archived ASC, synced_at DESC LIMIT 1",
                (p_key,),
            ).fetchone()
            if row is None or int(row[2]) != 0 or int(row[3]) != 0:
                return None
            ds_key = str(row[0])
            if not self._is_enabled_ds(ds_key):
                return None
            if not self._is_fresh(conn, ds_key, self._clock()):
                return None
            page: JSON = json.loads(str(row[1]))
            return page
        except (sqlite3.Error, OSError, json.JSONDecodeError, ValueError):
            return None
        finally:
            if conn is not None:
                conn.close()

    def retrieve_data_source(self, data_source_id: str) -> JSON | None:
        """Return mirrored data source metadata JSON for `data_source_id`, or None when unavailable/not ready/stale."""
        ds_key = normalize_page_id(data_source_id)
        if not self._is_enabled_ds(ds_key):
            return None
        conn: sqlite3.Connection | None = None
        try:
            conn = self._open_ro()
            if conn is None:
                return None
            row = conn.execute(
                "SELECT ready, last_sync_at, max_age_seconds, source_json FROM mirror_state WHERE data_source_id = ?",
                (ds_key,),
            ).fetchone()
            if row is None or not row[0] or row[1] is None or not row[3]:
                return None
            max_age = self._max_age_for(ds_key, float(row[2]) if row[2] is not None else None)
            if (self._clock() - float(row[1])) > max_age:
                return None
            meta = json.loads(str(row[3]))
            if not isinstance(meta, dict):
                return None
            return meta
        except (sqlite3.Error, OSError, json.JSONDecodeError, ValueError):
            return None
        finally:
            if conn is not None:
                conn.close()


@dataclass(frozen=True, slots=True)
class _SourceEntry:
    key: str
    id: str
    max_age_seconds: float | None = None


def _normalize_sources(
    data_sources: Mapping[str, Any],
    enabled_sources: Collection[str] | None,
) -> dict[str, _SourceEntry]:
    result: dict[str, _SourceEntry] = {}
    for key, value in sorted(data_sources.items(), key=lambda item: item[0]):
        if enabled_sources is not None and key not in enabled_sources:
            continue
        if isinstance(value, str):
            result[key] = _SourceEntry(key=key, id=value)
        else:
            ds_id = getattr(value, "id", None)
            if not isinstance(ds_id, str) or not ds_id:
                raise ValueError(f"invalid data source config for {key!r}")
            max_age = getattr(value, "max_age_seconds", None)
            result[key] = _SourceEntry(
                key=key,
                id=ds_id,
                max_age_seconds=float(max_age) if max_age is not None else None,
            )
    return result


class MirrorSyncer:
    """Paced incremental poll + staggered full rescan loop for mirrored Notion data sources."""

    def __init__(
        self,
        writer: MirrorWriter,
        notion: NotionSource | NotionClient,
        data_sources: Mapping[str, Any],
        *,
        enabled_sources: Collection[str] | None = None,
        poll_interval: float = DEFAULT_POLL_SECONDS,
        rescan_interval: float = DEFAULT_RESCAN_SECONDS,
        overlap_seconds: float = OVERLAP_SECONDS,
        min_request_interval: float = MIN_REQUEST_INTERVAL_SECONDS,
        pacer: RequestPacer | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] | None = None,
    ) -> None:
        from t6.notion.source import NotionSource, as_source

        self._writer = writer
        self._notion = as_source(notion)
        self._sources = _normalize_sources(data_sources, enabled_sources)
        self._poll_interval = float(poll_interval)
        self._rescan_interval = float(rescan_interval)
        self._overlap_seconds = float(overlap_seconds)
        self._min_request_interval = float(min_request_interval)
        self._clock = clock
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._log = log or (lambda line: print(line, file=sys.stderr, flush=True))
        self._pacer = pacer if pacer is not None else RequestPacer(self._min_request_interval, clock=clock, sleep=sleep)
        if type(self._notion) is NotionSource and getattr(self._notion, "_pacer", None) is None:
            self._notion._pacer = self._pacer
        self._last_poll_at: dict[str, float] = {}
        self._next_rescan_at: dict[str, float] = {}

    def _pace(self) -> None:
        if getattr(self._notion, "_pacer", None) is not self._pacer:
            self._pacer.wait()

    def _entry(self, source_key: str) -> _SourceEntry:
        if source_key not in self._sources:
            raise KeyError(f"mirror source {source_key!r} is not configured/enabled")
        return self._sources[source_key]

    def rescan_source(self, source_key: str, *, now: float | None = None) -> int:
        """Run a full `query_data_source` + `retrieve_data_source` rescan for `source_key`."""
        entry = self._entry(source_key)
        self._pace()
        pages = self._notion.query_data_source(entry.id, freshness="live")
        self._pace()
        meta = self._notion.retrieve_data_source(entry.id, freshness="live")
        synced_at = self._wall_clock() if now is None else now
        count = self._writer.replace_data_source(
            entry.id,
            pages,
            synced_at=synced_at,
            source_key=entry.key,
            source_meta=meta,
            max_age_seconds=entry.max_age_seconds,
        )
        self._last_poll_at[entry.key] = self._clock()
        return count

    def poll_source(self, source_key: str, *, now: float | None = None) -> int:
        """Poll `source_key` incrementally since `high_water_mark - overlap_seconds`, or rescan if not ready."""
        entry = self._entry(source_key)
        state = self._writer.state(entry.id)
        if state is None or not state.ready or not state.high_water_mark:
            return self.rescan_source(source_key, now=now)
        hwm_dt = _parse_iso(state.high_water_mark)
        if hwm_dt is None:
            return self.rescan_source(source_key, now=now)
        since = _format_iso_z(hwm_dt - timedelta(seconds=self._overlap_seconds))
        self._pace()
        pages = self._notion.query_data_source(
            entry.id,
            filter={"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": since}},
            freshness="live",
        )
        synced_at = self._wall_clock() if now is None else now
        count = self._writer.upsert_pages(
            entry.id,
            pages,
            synced_at=synced_at,
            source_key=entry.key,
        )
        self._last_poll_at[entry.key] = self._clock()
        return count

    def tick(self) -> None:
        """Advance mirror sync for all enabled sources, isolating per-source errors."""
        from t6.notion.errors import NotionError

        total = max(len(self._sources), 1)
        for index, (key, entry) in enumerate(self._sources.items()):
            stagger = (index * self._rescan_interval) / total
            try:
                state = self._writer.state(entry.id)
                mono_now = self._clock()
                if state is None or not state.ready or state.last_full_rescan_at is None:
                    self.rescan_source(key)
                    self._next_rescan_at[key] = self._clock() + self._rescan_interval + stagger
                    continue
                if key not in self._next_rescan_at:
                    wall_age = max(0.0, self._wall_clock() - state.last_full_rescan_at)
                    remaining = max(0.0, self._rescan_interval - wall_age)
                    self._next_rescan_at[key] = mono_now + remaining + stagger
                if key not in self._last_poll_at:
                    last_sync = state.last_sync_at or 0.0
                    wall_poll_age = max(0.0, self._wall_clock() - last_sync)
                    self._last_poll_at[key] = mono_now - min(wall_poll_age, self._poll_interval)
                if mono_now >= self._next_rescan_at[key]:
                    self.rescan_source(key)
                    self._next_rescan_at[key] = self._clock() + self._rescan_interval
                elif mono_now - self._last_poll_at[key] >= self._poll_interval:
                    self.poll_source(key)
            except (NotionError, OSError, sqlite3.Error, ValueError, RuntimeError) as error:
                self._log(f"t6.mirror: sync failed source={key} error={type(error).__name__}")
