"""Persistent practice state shared by Drive verbs and the nightly job, and the live setting (ADR 0010)."""
import logging
from collections.abc import Mapping

from t6.registry.sqlite import SqliteProjectRegistry

log = logging.getLogger("t6.drives")
LIVE_VARIABLE = "T6_DRIVE_LIVE"


def practice_mode(registry: SqliteProjectRegistry) -> bool:
    with registry._transaction() as db:
        return bool(db.execute('SELECT practice FROM drive_practice WHERE id=1').fetchone()[0])


def apply_live_setting(registry: SqliteProjectRegistry, environ: Mapping[str, str]) -> bool:
    """Read `T6_DRIVE_LIVE` at start. The first start with it on records live mode; it never goes back.

    Nothing is reloaded here. Returns True when the system is live (recorded now or earlier).
    """
    wanted = environ.get(LIVE_VARIABLE, "").strip().lower() in {"1", "true", "yes"}
    with registry._transaction() as db:
        row = db.execute('SELECT practice FROM drive_practice WHERE id=1').fetchone() if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='drive_practice'").fetchone() else None
        if row is None:
            return False  # a database without the Drive tables is not live
        practice = bool(row[0])
        if wanted and practice:
            db.execute('UPDATE drive_practice SET practice=0 WHERE id=1')
            log.warning("Drive live mode is on (%s): practice mode is off and reloads are refused", LIVE_VARIABLE)
            return True
        if not wanted and not practice:
            log.warning("%s is not set, but Drive live mode was recorded earlier: staying live (one way)", LIVE_VARIABLE)
        return not practice or wanted
