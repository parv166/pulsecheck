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
