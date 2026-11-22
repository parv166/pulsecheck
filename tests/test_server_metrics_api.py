import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.main import app


TEST_USERNAME = "servers-test-admin"
TEST_PASSWORD = "Correct-Servers-Test-2026!"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "PULSECHECK_DB_PATH",
        str(tmp_path / "servers-api.db"),
    )
    monkeypatch.setenv("PULSECHECK_ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv(
        "PULSECHECK_ADMIN_PASSWORD_HASH",
        hash_password(TEST_PASSWORD),
    )
    monkeypatch.setenv("PULSECHECK_COOKIE_SECURE", "false")
    monkeypatch.setenv(
        "PULSECHECK_METRICS_FILE",
        str(tmp_path / "metrics.json"),
    )

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


def test_servers_api_returns_snapshot(client, tmp_path):
    path = tmp_path / "metrics.json"

    snapshot = {
        "schema_version": 1,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "region": "ap-south-1",
        "instance": {
            "instance_id": "i-test123",
            "state": "running",
            "instance_type": "t3.small",
            "availability_zone": "ap-south-1a",
            "system_status": "ok",
            "instance_status": "ok",
        },
        "metrics": {
            "cpu_utilization": {"value": 12.5},
            "network_in_bytes": {"value": 2048},
            "network_out_bytes": {"value": 4096},
        },
        "collection": {"status": "ok", "errors": []},
    }

    path.write_text(json.dumps(snapshot), encoding="utf-8")

    response = client.get("/api/servers")

    assert response.status_code == 200

    data = response.json()
    assert data["available"] is True
    assert data["stale"] is False
    assert data["snapshot"]["instance"]["state"] == "running"
    assert data["snapshot"]["metrics"]["cpu_utilization"]["value"] == 12.5


def test_servers_api_reports_missing_snapshot(client):
    response = client.get("/api/servers")

    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["snapshot"] is None


def test_servers_api_requires_authentication(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "PULSECHECK_DB_PATH",
        str(tmp_path / "servers-anonymous.db"),
    )

    with TestClient(app) as anonymous_client:
        response = anonymous_client.get("/api/servers")

    assert response.status_code == 401
