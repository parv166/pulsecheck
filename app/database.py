import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
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

                CREATE TABLE IF NOT EXISTS incidents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_id TEXT NOT NULL,
                    target_name TEXT NOT NULL,
                    target_url TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK(status IN ('ACTIVE', 'RESOLVED', 'DISMISSED')),
                    started_at TEXT NOT NULL,
                    resolved_at TEXT,
                    status_code INTEGER,
                    latency_ms REAL NOT NULL,
                    consecutive_failures INTEGER NOT NULL,
                    error TEXT,
                    dismissed_at TEXT,
                    dismissal_reason TEXT
                );

                CREATE INDEX IF NOT EXISTS
                    idx_incidents_status_started
                ON incidents(status, started_at DESC);

                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_incidents_one_active_per_target
                ON incidents(target_id)
                WHERE status = 'ACTIVE';

                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    csrf_token TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS
                    idx_auth_sessions_expires
                ON auth_sessions(expires_at);
                """
            )

            self._migrate_incidents(connection)

    @staticmethod
    def _migrate_incidents(
        connection: sqlite3.Connection,
    ) -> None:
        """Add DISMISSED incidents while preserving existing records."""
        connection.execute("BEGIN IMMEDIATE")

        schema_row = connection.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'table' AND name = 'incidents'
            """
        ).fetchone()

        if schema_row is None:
            return

        schema = (schema_row["sql"] or "").upper()

        if "DISMISSED" not in schema:
            connection.execute(
                "DROP INDEX IF EXISTS idx_incidents_status_started"
            )
            connection.execute(
                "DROP INDEX IF EXISTS idx_incidents_one_active_per_target"
            )
            connection.execute(
                "ALTER TABLE incidents RENAME TO incidents_legacy"
            )
            connection.execute(
                """
                CREATE TABLE incidents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_id TEXT NOT NULL,
                    target_name TEXT NOT NULL,
                    target_url TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK(status IN ('ACTIVE', 'RESOLVED', 'DISMISSED')),
                    started_at TEXT NOT NULL,
                    resolved_at TEXT,
                    status_code INTEGER,
                    latency_ms REAL NOT NULL,
                    consecutive_failures INTEGER NOT NULL,
                    error TEXT,
                    dismissed_at TEXT,
                    dismissal_reason TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT INTO incidents (
                    id, target_id, target_name, target_url, status,
                    started_at, resolved_at, status_code, latency_ms,
                    consecutive_failures, error
                )
                SELECT
                    id, target_id, target_name, target_url, status,
                    started_at, resolved_at, status_code, latency_ms,
                    consecutive_failures, error
                FROM incidents_legacy
                """
            )
            connection.execute("DROP TABLE incidents_legacy")
        else:
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(incidents)"
                ).fetchall()
            }
            if "dismissed_at" not in columns:
                connection.execute(
                    "ALTER TABLE incidents ADD COLUMN dismissed_at TEXT"
                )
            if "dismissal_reason" not in columns:
                connection.execute(
                    "ALTER TABLE incidents ADD COLUMN dismissal_reason TEXT"
                )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_incidents_status_started
            ON incidents(status, started_at DESC)
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                idx_incidents_one_active_per_target
            ON incidents(target_id)
            WHERE status = 'ACTIVE'
            """
        )

        # Preserve incidents whose monitors were deleted by older versions.
        migration_time = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """
            UPDATE incidents
            SET status = 'DISMISSED',
                dismissed_at = ?,
                dismissal_reason = ?
            WHERE status = 'ACTIVE'
              AND NOT EXISTS (
                  SELECT 1
                  FROM monitored_targets AS target
                  WHERE target.id = incidents.target_id
              )
            """,
            (
                migration_time,
                "Monitor was deleted before incident dismissal tracking was added",
            ),
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
            exists = connection.execute(
                "SELECT 1 FROM monitored_targets WHERE id = ?",
                (target_id,),
            ).fetchone()

            if exists is None:
                return False

            dismissed_at = datetime.now(timezone.utc).isoformat()
            connection.execute(
                """
                UPDATE incidents
                SET status = 'DISMISSED',
                    dismissed_at = ?,
                    dismissal_reason = ?
                WHERE target_id = ? AND status = 'ACTIVE'
                """,
                (
                    dismissed_at,
                    "Monitor deleted while DOWN",
                    target_id,
                ),
            )

            cursor = connection.execute(
                "DELETE FROM monitored_targets WHERE id = ?",
                (target_id,),
            )

        return cursor.rowcount > 0

    def save_result(self, result: CheckResult) -> None:
        self.save_results([result])

    def save_results(self, results: list[CheckResult]) -> None:
        if not results:
            return

        records = [
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
            )
            for result in results
        ]

        with self._connection() as connection:
            connection.executemany(
                """
                INSERT INTO monitoring_history (
                    target_id, status, status_code, latency_ms,
                    checked_at, consecutive_failures,
                    last_success_at, error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                records,
            )

            # Maintain incident state on every persisted observation.
            for result in results:
                checked_at = result.checked_at.isoformat()

                active = connection.execute(
                    """
                    SELECT id FROM incidents
                    WHERE target_id = ? AND status = 'ACTIVE'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (result.target_id,),
                ).fetchone()

                if result.status == "DOWN":
                    if active is None:
                        connection.execute(
                            """
                            INSERT INTO incidents (
                                target_id, target_name, target_url,
                                status, started_at, status_code,
                                latency_ms, consecutive_failures, error
                            )
                            VALUES (?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?)
                            """,
                            (
                                result.target_id,
                                result.name,
                                result.url,
                                checked_at,
                                result.status_code,
                                result.latency_ms,
                                result.consecutive_failures,
                                result.error,
                            ),
                        )
                    else:
                        connection.execute(
                            """
                            UPDATE incidents
                            SET status_code = ?,
                                latency_ms = ?,
                                consecutive_failures = ?,
                                error = ?
                            WHERE id = ?
                            """,
                            (
                                result.status_code,
                                result.latency_ms,
                                result.consecutive_failures,
                                result.error,
                                active["id"],
                            ),
                        )

                elif result.status == "UP" and active is not None:
                    connection.execute(
                        """
                        UPDATE incidents
                        SET status = 'RESOLVED',
                            resolved_at = ?,
                            status_code = ?,
                            latency_ms = ?,
                            consecutive_failures = 0,
                            error = NULL
                        WHERE id = ?
                        """,
                        (
                            checked_at,
                            result.status_code,
                            result.latency_ms,
                            active["id"],
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


    def list_incidents(
        self,
        status: str = "all",
        limit: int = 100,
    ) -> list[dict]:
        if status not in {"all", "active", "resolved", "dismissed"}:
            raise ValueError("Invalid incident status.")

        if not 1 <= limit <= 200:
            raise ValueError("Incident limit must be between 1 and 200.")

        filter_status = {
            "active": "ACTIVE",
            "resolved": "RESOLVED",
            "dismissed": "DISMISSED",
        }.get(status)

        with self._connection() as connection:
            if filter_status:
                rows = connection.execute(
                    """
                    SELECT * FROM incidents
                    WHERE status = ?
                    ORDER BY started_at DESC, id DESC
                    LIMIT ?
                    """,
                    (filter_status, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM incidents
                    ORDER BY started_at DESC, id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()

        return [dict(row) for row in rows]

    def get_incident_summary(self) -> dict[str, int]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM incidents
                GROUP BY status
                """
            ).fetchall()

        counts = {row["status"]: row["count"] for row in rows}
        active = counts.get("ACTIVE", 0)
        resolved = counts.get("RESOLVED", 0)
        dismissed = counts.get("DISMISSED", 0)

        return {
            "active": active,
            "resolved": resolved,
            "dismissed": dismissed,
            "total": active + resolved + dismissed,
        }

    def create_session(
        self,
        token_hash: str,
        username: str,
        csrf_token: str,
        created_at: str,
        expires_at: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO auth_sessions (
                    token_hash,
                    username,
                    csrf_token,
                    created_at,
                    expires_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    token_hash,
                    username,
                    csrf_token,
                    created_at,
                    expires_at,
                ),
            )

    def get_session(self, token_hash: str) -> dict | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT token_hash, username, csrf_token,
                       created_at, expires_at
                FROM auth_sessions
                WHERE token_hash = ?
                """,
                (token_hash,),
            ).fetchone()

        return dict(row) if row is not None else None

    def delete_session(self, token_hash: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM auth_sessions WHERE token_hash = ?",
                (token_hash,),
            )

        return cursor.rowcount > 0

    def delete_expired_sessions(self, now_iso: str) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM auth_sessions WHERE expires_at <= ?",
                (now_iso,),
            )

        return cursor.rowcount
