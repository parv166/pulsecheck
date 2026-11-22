import sqlite3
from datetime import datetime, timezone

from app.database import Database
from app.monitoring import CheckResult, MonitoredTarget


def create_target():
    return MonitoredTarget(
        id="test-target-1",
        name="Demo Application",
        url="https://example.com/health",
    )


def create_result(target):
    now = datetime.now(timezone.utc)

    return CheckResult(
        target_id=target.id,
        name=target.name,
        url=target.url,
        status="UP",
        status_code=200,
        latency_ms=45.5,
        checked_at=now,
        consecutive_failures=0,
        last_success_at=now,
        error=None,
    )


def test_database_creates_tables(tmp_path):
    db = Database(tmp_path / "pulsecheck.db")

    with db._connection() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                """
            ).fetchall()
        }

    assert "monitored_targets" in tables
    assert "monitoring_history" in tables


def test_save_and_get_target(tmp_path):
    db = Database(tmp_path / "pulsecheck.db")
    target = create_target()

    db.save_target(target)

    assert db.get_target(target.id) == target


def test_list_targets(tmp_path):
    db = Database(tmp_path / "pulsecheck.db")
    target = create_target()

    db.save_target(target)

    assert db.list_targets() == [target]


def test_save_and_get_latest_result(tmp_path):
    db = Database(tmp_path / "pulsecheck.db")
    target = create_target()
    result = create_result(target)

    db.save_target(target)
    db.save_result(result)

    stored = db.get_latest_result(target.id)

    assert stored == result


def test_monitoring_history(tmp_path):
    db = Database(tmp_path / "pulsecheck.db")
    target = create_target()

    db.save_target(target)

    first = create_result(target)
    db.save_result(first)

    second = create_result(target)
    db.save_result(second)

    history = db.list_history(target.id, limit=10)

    assert len(history) == 2
    assert history[0] == second
    assert history[1] == first


def test_delete_target_cascades_history(tmp_path):
    db = Database(tmp_path / "pulsecheck.db")
    target = create_target()

    db.save_target(target)
    db.save_result(create_result(target))

    assert db.delete_target(target.id) is True
    assert db.get_target(target.id) is None
    assert db.get_latest_result(target.id) is None
    assert db.delete_target(target.id) is False



def test_delete_target_dismisses_active_incident(tmp_path):
    db = Database(tmp_path / "pulsecheck.db")
    target = create_target()
    now = datetime.now(timezone.utc)

    down_result = CheckResult(
        target_id=target.id,
        name=target.name,
        url=target.url,
        status="DOWN",
        status_code=503,
        latency_ms=45.5,
        checked_at=now,
        consecutive_failures=2,
        last_success_at=None,
        error="Unexpected HTTP status: 503",
    )

    db.save_target(target)
    db.save_result(down_result)

    assert db.get_incident_summary()["active"] == 1
    assert db.delete_target(target.id) is True

    incidents = db.list_incidents()
    assert len(incidents) == 1
    assert incidents[0]["status"] == "DISMISSED"
    assert incidents[0]["dismissed_at"] is not None
    assert incidents[0]["resolved_at"] is None
    assert incidents[0]["dismissal_reason"] == "Monitor deleted while DOWN"

    assert db.get_incident_summary() == {
        "active": 0,
        "resolved": 0,
        "dismissed": 1,
        "total": 1,
    }
    assert db.list_incidents("dismissed") == incidents
    assert db.list_history(target.id) == []


def test_initialize_migrates_legacy_orphaned_active_incident(tmp_path):
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE monitored_targets (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        INSERT INTO monitored_targets (id, name, url)
        VALUES ('still-monitored', 'Current monitor', 'https://example.com');

        CREATE TABLE incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_id TEXT NOT NULL,
            target_name TEXT NOT NULL,
            target_url TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'RESOLVED')),
            started_at TEXT NOT NULL,
            resolved_at TEXT,
            status_code INTEGER,
            latency_ms REAL NOT NULL,
            consecutive_failures INTEGER NOT NULL,
            error TEXT
        );

        CREATE INDEX idx_incidents_status_started
        ON incidents(status, started_at DESC);

        CREATE UNIQUE INDEX idx_incidents_one_active_per_target
        ON incidents(target_id)
        WHERE status = 'ACTIVE';

        INSERT INTO incidents (
            id, target_id, target_name, target_url, status, started_at,
            resolved_at, status_code, latency_ms, consecutive_failures, error
        )
        VALUES
            (1, 'still-monitored', 'Current monitor',
             'https://example.com', 'ACTIVE', '2026-11-22T10:00:00+00:00',
             NULL, 503, 40.0, 2, 'Still down'),
            (2, 'deleted-monitor', 'Deleted monitor',
             'https://httpbin.org/status/503', 'ACTIVE',
             '2026-11-22T10:01:00+00:00', NULL, 503, 50.0, 3, 'Monitor deleted'),
            (3, 'still-monitored', 'Current monitor',
             'https://example.com', 'RESOLVED',
             '2026-11-22T09:00:00+00:00',
             '2026-11-22T09:05:00+00:00', 200, 20.0, 0, NULL);
        """
    )
    connection.close()

    db = Database(db_path)
    incidents = {item["id"]: item for item in db.list_incidents()}

    assert len(incidents) == 3
    assert incidents[1]["status"] == "ACTIVE"
    assert incidents[2]["status"] == "DISMISSED"
    assert incidents[2]["dismissed_at"] is not None
    assert incidents[2]["dismissal_reason"] == (
        "Monitor was deleted before incident dismissal tracking was added"
    )
    assert incidents[3]["status"] == "RESOLVED"

    assert db.get_incident_summary() == {
        "active": 1,
        "resolved": 1,
        "dismissed": 1,
        "total": 3,
    }


def test_save_results_batch(tmp_path):
    db = Database(tmp_path / "pulsecheck.db")
    target = create_target()

    db.save_target(target)

    first = create_result(target)
    second = create_result(target)

    db.save_results([first, second])

    history = db.list_history(target.id, limit=10)

    assert len(history) == 2


def test_save_results_empty_list(tmp_path):
    db = Database(tmp_path / "pulsecheck.db")

    db.save_results([])

    assert db.list_targets() == []
