"""Opening a registry stays fast as audit history grows (#502).

Every registry open runs the activity backfill under the write lock. Without an
index on activity.audit_id its NOT EXISTS check scanned activity once per audit
row, so each open took time quadratic in history and blocked the API: the
write lock is held for the whole open, so the open time is the time other
writers and /health wait.
"""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from t6.registry.sqlite import SqliteProjectRegistry

ROWS = 20_000
BUDGET = 2.0  # seconds; about 0.02s with the index, about 6s without it


class RegistryOpenTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'registry.sqlite'
        SqliteProjectRegistry(self.path)
        # llm run writes audit rows with no activity row; the next open copies them in.
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executemany("INSERT INTO audit (project_id, action, actor, at, before, after) VALUES ('svc', 'llm_run', 'svc', ?, NULL, '{}')",
                           [(float(i),) for i in range(ROWS)])
        SqliteProjectRegistry(self.path)

    def counts(self) -> tuple[int, int]:
        with closing(sqlite3.connect(self.path)) as db:
            return (db.execute('SELECT COUNT(*) FROM audit').fetchone()[0],
                    db.execute('SELECT COUNT(*) FROM activity').fetchone()[0])

    def test_backfill_looks_up_activity_by_index(self) -> None:
        with closing(sqlite3.connect(self.path)) as db:
            plan = [row[3] for row in db.execute(
                'EXPLAIN QUERY PLAN SELECT id FROM audit WHERE NOT EXISTS (SELECT 1 FROM activity WHERE activity.audit_id = audit.id)')]
        self.assertNotIn('SCAN activity', plan)
        self.assertTrue(any('activity_by_audit' in step for step in plan), plan)

    def test_reopening_with_existing_activity_is_fast_and_copies_nothing(self) -> None:
        self.assertEqual(self.counts(), (ROWS, ROWS))
        started = time.perf_counter()
        SqliteProjectRegistry(self.path)
        SqliteProjectRegistry(self.path)
        self.assertLess(time.perf_counter() - started, BUDGET)
        self.assertEqual(self.counts(), (ROWS, ROWS))

    def test_new_audit_rows_are_still_backfilled_once(self) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO audit (project_id, action, actor, at, before, after) VALUES ('svc', 'llm_run', 'svc', 1e9, NULL, '{}')")
        SqliteProjectRegistry(self.path)
        SqliteProjectRegistry(self.path)
        self.assertEqual(self.counts(), (ROWS + 1, ROWS + 1))

    def test_the_previous_release_opens_a_file_with_the_index(self) -> None:
        from t6.registry import sqlite as migrations
        with patch.object(migrations, '_MIGRATIONS', migrations._MIGRATIONS[:-1]):
            SqliteProjectRegistry(self.path)
        self.assertEqual(self.counts(), (ROWS, ROWS))


if __name__ == '__main__':
    unittest.main()
