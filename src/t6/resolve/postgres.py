"""Connect to the Resolve project server (PostgreSQL) with a read-only role.

Needs `pip install 't6[resolve]'`. Not covered by tests: they have no live
server, so the SQL here is checked only by reading it against the PostgreSQL
documentation. Run it once against the real server before relying on it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

# Superuser, create rights on the database, or any write right on any table.
_CAN_WRITE = """
SELECT
  COALESCE((SELECT rolsuper FROM pg_roles WHERE rolname = current_user), false)
  OR has_database_privilege(current_user, current_database(), 'CREATE')
  OR EXISTS (
    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND (has_table_privilege(c.oid, 'INSERT') OR has_table_privilege(c.oid, 'UPDATE')
        OR has_table_privilege(c.oid, 'DELETE') OR has_table_privilege(c.oid, 'TRUNCATE'))
  )
"""


class PostgresDatabase:
    """A `Database` over psycopg. The session is also set read-only, as a second guard."""

    def __init__(self, dsn: str, database: str | None = None) -> None:
        try:
            import psycopg
        except ImportError as error:  # pragma: no cover
            raise ImportError("install 't6[resolve]' to read the project server") from error
        kwargs: dict[str, Any] = {"autocommit": True}
        if database is not None:
            kwargs["dbname"] = database
        self._conn = psycopg.connect(dsn, options="-c default_transaction_read_only=on", **kwargs)

    def can_write(self) -> bool:
        row = self._conn.execute(_CAN_WRITE).fetchone()
        return bool(row and row[0])

    def query(self, sql: str) -> Sequence[Sequence[Any]]:
        return self._conn.execute(sql).fetchall()

    def close(self) -> None:
        self._conn.close()
