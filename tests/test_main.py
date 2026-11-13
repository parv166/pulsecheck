from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_home():
    response = client.get("/")

    assert response.status_code == 200
    assert response.json()["application"] == "PulseCheck"


def test_health():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_version():
    response = client.get("/version")

    assert response.status_code == 200
    assert response.json()["version"] == "1.0.0"


def test_demo():
    response = client.get("/api/demo")

    assert response.status_code == 200
    assert response.json()["result"] == "success"



def test_metrics():
    # Generate a request so the metrics have data.
    client.get("/health")

    response = client.get("/metrics")

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]

    metrics = response.text

    assert "pulsecheck_http_requests_total" in metrics
    assert 'method="GET"' in metrics
    assert 'path="/health"' in metrics
    assert 'status_code="200"' in metrics
    assert "pulsecheck_http_request_duration_seconds_bucket" in metrics
