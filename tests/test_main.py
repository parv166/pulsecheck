from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.monitoring import CheckResult, MonitoringService


@pytest.fixture
def client():
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
