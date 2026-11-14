import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from app.monitoring import CheckResult, MonitoredTarget


class Database:

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            str(self.db_path),
            timeout=10.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")

        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")

            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS monitored_targets (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS monitoring_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_id TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK(status IN ('UP', 'DOWN')),
                    status_code INTEGER,
                    latency_ms REAL NOT NULL,
                    checked_at TEXT NOT NULL,
                    consecutive_failures INTEGER NOT NULL,
                    last_success_at TEXT,
                    error TEXT,
                    FOREIGN KEY(target_id)
                        REFERENCES monitored_targets(id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS
                    idx_monitoring_history_target_checked
                ON monitoring_history(target_id, checked_at DESC);
                """
            )

    def save_target(self, target: MonitoredTarget) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO monitored_targets (id, name, url)
                VALUES (?, ?, ?)
                """,
                (target.id, target.name, target.url),
            )

    def get_target(self, target_id: str) -> MonitoredTarget | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id, name, url
                FROM monitored_targets
                WHERE id = ?
                """,
                (target_id,),
            ).fetchone()

        if row is None:
            return None

        return MonitoredTarget(
            id=row["id"],
            name=row["name"],
            url=row["url"],
        )

    def list_targets(self) -> list[MonitoredTarget]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, name, url
                FROM monitored_targets
                ORDER BY created_at ASC
                """
            ).fetchall()

        return [
            MonitoredTarget(
                id=row["id"],
                name=row["name"],
                url=row["url"],
            )
            for row in rows
        ]

    def delete_target(self, target_id: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM monitored_targets WHERE id = ?",
                (target_id,),
            )

        return cursor.rowcount > 0

    def save_result(self, result: CheckResult) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO monitoring_history (
                    target_id,
                    status,
                    status_code,
                    latency_ms,
                    checked_at,
                    consecutive_failures,
                    last_success_at,
                    error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.target_id,
                    result.status,
                    result.status_code,
                    result.latency_ms,
                    result.checked_at.isoformat(),
                    result.consecutive_failures,
                    (
                        result.last_success_at.isoformat()
                        if result.last_success_at
                        else None
                    ),
                    result.error,
                ),
            )

    @staticmethod
    def _row_to_result(row: sqlite3.Row) -> CheckResult:
        return CheckResult(
            target_id=row["target_id"],
            name=row["name"],
            url=row["url"],
            status=row["status"],
            status_code=row["status_code"],
            latency_ms=row["latency_ms"],
            checked_at=datetime.fromisoformat(row["checked_at"]),
            consecutive_failures=row["consecutive_failures"],
            last_success_at=(
                datetime.fromisoformat(row["last_success_at"])
                if row["last_success_at"]
                else None
            ),
            error=row["error"],
        )

    def get_latest_result(
        self,
        target_id: str,
    ) -> CheckResult | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT
                    h.*,
                    t.name,
                    t.url
                FROM monitoring_history AS h
                JOIN monitored_targets AS t
                    ON t.id = h.target_id
                WHERE h.target_id = ?
                ORDER BY h.id DESC
                LIMIT 1
                """,
                (target_id,),
            ).fetchone()

        if row is None:
            return None

        return self._row_to_result(row)

    def list_history(
        self,
        target_id: str,
        limit: int = 100,
    ) -> list[CheckResult]:
        if not 1 <= limit <= 1000:
            raise ValueError("History limit must be between 1 and 1000.")

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    h.*,
                    t.name,
                    t.url
                FROM monitoring_history AS h
                JOIN monitored_targets AS t
                    ON t.id = h.target_id
                WHERE h.target_id = ?
                ORDER BY h.id DESC
                LIMIT ?
                """,
                (target_id, limit),
            ).fetchall()

        return [self._row_to_result(row) for row in rows]
