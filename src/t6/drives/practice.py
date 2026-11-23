"""Persistent practice state shared by Drive verbs and the nightly job."""
from t6.registry.sqlite import SqliteProjectRegistry


def practice_mode(registry: SqliteProjectRegistry) -> bool:
    with registry._transaction() as db:
        return bool(db.execute('SELECT practice FROM drive_practice WHERE id=1').fetchone()[0])
