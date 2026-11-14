from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.monitoring import CheckResult, MonitoringService


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "PULSECHECK_DB_PATH",
        str(tmp_path / "pulsecheck-test.db"),
    )

    with TestClient(app) as test_client:
        yield test_client


def test_home(client):
    response = client.get("/")

    assert response.status_code == 200
    assert response.json()["application"] == "PulseCheck"


def test_health(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_version(client):
    response = client.get("/version")

    assert response.status_code == 200
    assert response.json()["version"] == "1.0.0"


def test_demo(client):
    response = client.get("/api/demo")

    assert response.status_code == 200
    assert response.json()["result"] == "success"


def test_metrics(client):
    client.get("/health")

    response = client.get("/metrics")

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "pulsecheck_http_requests_total" in response.text
    assert 'method="GET"' in response.text


def test_create_target(client):
    response = client.post(
        "/api/targets",
        json={
            "name": "Demo Application",
            "url": "https://example.com/health",
        },
    )

    assert response.status_code == 201
    assert response.json()["name"] == "Demo Application"
    assert response.json()["url"] == "https://example.com/health"


def test_list_targets(client):
    client.post(
        "/api/targets",
        json={
            "name": "Demo Application",
            "url": "https://example.com/health",
        },
    )

    response = client.get("/api/targets")

    assert response.status_code == 200
    assert len(response.json()["targets"]) == 1


def test_get_target(client):
    created = client.post(
        "/api/targets",
        json={
            "name": "Demo Application",
            "url": "https://example.com/health",
        },
    ).json()

    response = client.get(
        f"/api/targets/{created['id']}"
    )

    assert response.status_code == 200
    assert response.json()["target"]["id"] == created["id"]


def test_reject_invalid_target(client):
    response = client.post(
        "/api/targets",
        json={
            "name": "Unsafe Application",
            "url": "http://127.0.0.1:8000/health",
        },
    )

    assert response.status_code == 422


def test_delete_target(client):
    created = client.post(
        "/api/targets",
        json={
            "name": "Demo Application",
            "url": "https://example.com/health",
        },
    ).json()

    response = client.delete(
        f"/api/targets/{created['id']}"
    )

    assert response.status_code == 200
    assert response.json()["deleted"] is True


def test_manual_check(client, monkeypatch):
    created = client.post(
        "/api/targets",
        json={
            "name": "Demo Application",
            "url": "https://example.com/health",
        },
    ).json()

    async def fake_check_target(service, target_id):
        target = next(
            item
            for item in service.list_targets()
            if item.id == target_id
        )

        return CheckResult(
            target_id=target.id,
            name=target.name,
            url=target.url,
            status="UP",
            status_code=200,
            latency_ms=12.5,
            checked_at=datetime.now(timezone.utc),
            consecutive_failures=0,
            last_success_at=datetime.now(timezone.utc),
            error=None,
        )

    monkeypatch.setattr(
        MonitoringService,
        "check_target",
        fake_check_target,
    )

    response = client.post(
        f"/api/targets/{created['id']}/check"
    )

    assert response.status_code == 200
    assert response.json()["status"] == "UP"


def test_unknown_target_returns_404(client):
    response = client.get(
        "/api/targets/nonexistent"
    )

    assert response.status_code == 404
def test_scheduler_lifecycle():
    with TestClient(app) as test_client:
        scheduler_task = (
            test_client.app.state.monitoring_scheduler_task
        )

        assert scheduler_task is not None
        assert not scheduler_task.done()

    assert scheduler_task.done()
    assert scheduler_task.cancelled()


def test_history_endpoint_returns_persisted_check(client, monkeypatch):
    created = client.post(
        "/api/targets",
        json={
            "name": "History Test Application",
            "url": "https://example.com/health",
        },
    ).json()

    async def fake_check_target(service, target_id):
        target = next(
            item
            for item in service.list_targets()
            if item.id == target_id
        )

        now = datetime.now(timezone.utc)

        return CheckResult(
            target_id=target.id,
            name=target.name,
            url=target.url,
            status="UP",
            status_code=200,
            latency_ms=25.5,
            checked_at=now,
            consecutive_failures=0,
            last_success_at=now,
            error=None,
        )

    monkeypatch.setattr(
        MonitoringService,
        "check_target",
        fake_check_target,
    )

    check_response = client.post(
        f"/api/targets/{created['id']}/check"
    )

    assert check_response.status_code == 200

    history_response = client.get(
        f"/api/targets/{created['id']}/history"
    )

    assert history_response.status_code == 200

    history = history_response.json()["history"]

    assert len(history) == 1
    assert history[0]["status"] == "UP"
    assert history[0]["status_code"] == 200


def test_target_and_history_survive_application_restart(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "restart-test.db"

    monkeypatch.setenv(
        "PULSECHECK_DB_PATH",
        str(database_path),
    )

    async def fake_check_target(service, target_id):
        target = next(
            item
            for item in service.list_targets()
            if item.id == target_id
        )

        now = datetime.now(timezone.utc)

        return CheckResult(
            target_id=target.id,
            name=target.name,
            url=target.url,
            status="UP",
            status_code=200,
            latency_ms=30.0,
            checked_at=now,
            consecutive_failures=0,
            last_success_at=now,
            error=None,
        )

    monkeypatch.setattr(
        MonitoringService,
        "check_target",
        fake_check_target,
    )

    with TestClient(app) as first_client:
        created = first_client.post(
            "/api/targets",
            json={
                "name": "Persistent Application",
                "url": "https://example.com/health",
            },
        )

        assert created.status_code == 201

        target_id = created.json()["id"]

        check_response = first_client.post(
            f"/api/targets/{target_id}/check"
        )

        assert check_response.status_code == 200

    # Simulate application shutdown and a fresh startup.
    with TestClient(app) as second_client:
        targets_response = second_client.get("/api/targets")

        assert targets_response.status_code == 200

        targets = targets_response.json()["targets"]

        assert len(targets) == 1
        assert targets[0]["target"]["id"] == target_id
        assert targets[0]["last_check"]["status"] == "UP"

        history_response = second_client.get(
            f"/api/targets/{target_id}/history"
        )

        assert history_response.status_code == 200
        assert len(history_response.json()["history"]) == 1


def test_monitored_target_metrics_exposed(client, monkeypatch):
    created = client.post(
        "/api/targets",
        json={
            "name": "Metrics Test",
            "url": "https://example.com/health",
        },
    ).json()

    async def fake_check_target(service, target_id):
        target = next(
            item
            for item in service.list_targets()
            if item.id == target_id
        )

        now = datetime.now(timezone.utc)

        result = CheckResult(
            target_id=target.id,
            name=target.name,
            url=target.url,
            status="UP",
            status_code=200,
            latency_ms=125.0,
            checked_at=now,
            consecutive_failures=0,
            last_success_at=now,
            error=None,
        )

        # Mimic the state update performed by the real check method.
        service._results[target_id] = result

        return result

    monkeypatch.setattr(
        MonitoringService,
        "check_target",
        fake_check_target,
    )

    check = client.post(
        f"/api/targets/{created['id']}/check"
    )

    assert check.status_code == 200

    response = client.get("/metrics")

    assert response.status_code == 200

    metrics = response.text

    assert "pulsecheck_monitored_target_up" in metrics
    assert "pulsecheck_monitored_target_response_time_seconds" in metrics
    assert "pulsecheck_monitored_target_consecutive_failures" in metrics
    assert "pulsecheck_monitored_target_last_check_timestamp_seconds" in metrics
    assert 'target_name="Metrics Test"' in metrics
