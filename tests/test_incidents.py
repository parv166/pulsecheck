from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.database import Database
from app.main import app
from app.monitoring import CheckResult, MonitoredTarget, MonitoringService


TEST_USERNAME = "incident-test-admin"
TEST_PASSWORD = "Correct-Incident-Test-2026!"


def make_result(target, status, minute, failures=0):
    checked_at = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)
    checked_at += timedelta(minutes=minute)

    return CheckResult(
        target_id=target.id,
        name=target.name,
        url=target.url,
        status=status,
        status_code=503 if status == "DOWN" else 200,
        latency_ms=45.0,
        checked_at=checked_at,
        consecutive_failures=failures,
        last_success_at=(
            checked_at if status == "UP" else None
        ),
        error=(
            "Unexpected HTTP status: 503"
            if status == "DOWN"
            else None
        ),
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "PULSECHECK_DB_PATH",
        str(tmp_path / "incidents-api.db"),
    )
    monkeypatch.setenv("PULSECHECK_ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv(
        "PULSECHECK_ADMIN_PASSWORD_HASH",
        hash_password(TEST_PASSWORD),
    )
    monkeypatch.setenv("PULSECHECK_COOKIE_SECURE", "false")

    with TestClient(app) as test_client:
        login = test_client.post(
            "/api/auth/login",
            json={
                "username": TEST_USERNAME,
                "password": TEST_PASSWORD,
            },
        )
        assert login.status_code == 200

        test_client.headers["X-CSRF-Token"] = (
            login.json()["csrf_token"]
        )
        yield test_client


def test_incident_down_recovery_and_recurrence(tmp_path):
    database = Database(tmp_path / "incident-lifecycle.db")
    target = MonitoredTarget(
        id="target-incident-1",
        name="Production API",
        url="https://example.com/health",
    )
    database.save_target(target)

    first_down = make_result(target, "DOWN", 0, failures=1)
    database.save_result(first_down)

    active = database.list_incidents("active")
    assert len(active) == 1
    assert active[0]["status"] == "ACTIVE"
    assert active[0]["target_name"] == "Production API"

    # Repeated DOWN updates the active incident rather than duplicating it.
    repeated_down = make_result(target, "DOWN", 1, failures=2)
    database.save_result(repeated_down)

    active = database.list_incidents("active")
    assert len(active) == 1
    assert active[0]["consecutive_failures"] == 2

    # Recovery resolves the existing incident.
    recovered = make_result(target, "UP", 2)
    database.save_result(recovered)

    assert database.list_incidents("active") == []

    resolved = database.list_incidents("resolved")
    assert len(resolved) == 1
    assert resolved[0]["resolved_at"] == recovered.checked_at.isoformat()

    # A later outage creates a new incident.
    second_down = make_result(target, "DOWN", 3, failures=1)
    database.save_result(second_down)

    assert len(database.list_incidents("active")) == 1
    assert database.get_incident_summary() == {
        "active": 1,
        "resolved": 1,
        "total": 2,
    }

    # Incident history survives deletion of the original monitor.
    assert database.delete_target(target.id)
    assert len(database.list_incidents("all")) == 2


def test_incident_api_filters_and_summary(client):
    database = client.app.state.database

    target = MonitoredTarget(
        id="target-api-1",
        name="API Test",
        url="https://example.com/health",
    )
    database.save_target(target)

    down = make_result(target, "DOWN", 0, failures=1)
    database.save_result(down)

    active_response = client.get("/api/incidents?status=active")
    assert active_response.status_code == 200
    assert active_response.json()["count"] == 1
    assert active_response.json()["incidents"][0]["status"] == "ACTIVE"

    recovered = make_result(target, "UP", 1)
    database.save_result(recovered)

    resolved_response = client.get("/api/incidents?status=resolved")
    assert resolved_response.status_code == 200
    assert resolved_response.json()["count"] == 1
    assert resolved_response.json()["incidents"][0]["status"] == "RESOLVED"

    summary = client.get("/api/incidents/summary")
    assert summary.status_code == 200
    assert summary.json() == {
        "active": 0,
        "resolved": 1,
        "total": 1,
    }


def test_incident_api_rejects_invalid_filters(client):
    response = client.get("/api/incidents?status=invalid")
    assert response.status_code == 422

    response = client.get("/api/incidents?limit=500")
    assert response.status_code == 422


def test_incident_api_requires_authentication(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "PULSECHECK_DB_PATH",
        str(tmp_path / "anonymous-incidents.db"),
    )
    monkeypatch.setenv("PULSECHECK_ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv(
        "PULSECHECK_ADMIN_PASSWORD_HASH",
        hash_password(TEST_PASSWORD),
    )
    monkeypatch.setenv("PULSECHECK_COOKIE_SECURE", "false")

    with TestClient(app) as anonymous:
        assert anonymous.get("/api/incidents").status_code == 401
        assert anonymous.get("/api/incidents/summary").status_code == 401


def test_manual_check_persists_down_and_recovery(client, monkeypatch):
    state = {"status": "DOWN", "minute": 0}

    async def fake_check_target(service, target_id):
        target = next(
            item for item in service.list_targets()
            if item.id == target_id
        )

        current = make_result(
            target,
            state["status"],
            state["minute"],
            failures=1 if state["status"] == "DOWN" else 0,
        )

        service._results[target_id] = current

        return current

    monkeypatch.setattr(
        MonitoringService,
        "check_target",
        fake_check_target,
    )

    created = client.post(
        "/api/targets",
        json={
            "name": "Callback Integration Test",
            "url": "https://example.com/health",
        },
    )
    assert created.status_code == 201
    target_id = created.json()["id"]

    down = client.post(f"/api/targets/{target_id}/check")
    assert down.status_code == 200
    assert down.json()["status"] == "DOWN"

    active = client.get("/api/incidents?status=active").json()
    assert active["count"] == 1
    assert active["incidents"][0]["target_id"] == target_id

    state["status"] = "UP"
    state["minute"] = 1

    recovered = client.post(f"/api/targets/{target_id}/check")
    assert recovered.status_code == 200
    assert recovered.json()["status"] == "UP"

    resolved = client.get("/api/incidents?status=resolved").json()
    assert resolved["count"] == 1
    assert resolved["incidents"][0]["resolved_at"] is not None

    assert client.get("/api/incidents/summary").json() == {
        "active": 0,
        "resolved": 1,
        "total": 1,
    }
