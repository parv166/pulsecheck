import json
from datetime import datetime, timedelta, timezone

from app.server_metrics import load_server_metrics


NOW = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)


def make_snapshot(collected_at=None):
    return {
        "schema_version": 1,
        "collected_at": (
            collected_at or NOW.isoformat()
        ),
        "region": "ap-south-1",
        "instance": {
            "instance_id": "i-test123",
            "state": "running",
        },
        "metrics": {
            "cpu_utilization": {"value": 12.5},
            "network_in_bytes": {"value": 2048},
            "network_out_bytes": {"value": 4096},
        },
        "collection": {
            "status": "ok",
            "errors": [],
        },
    }


def test_load_fresh_snapshot(tmp_path):
    path = tmp_path / "metrics.json"
    path.write_text(json.dumps(make_snapshot()), encoding="utf-8")

    result = load_server_metrics(path, now=NOW)

    assert result["available"] is True
    assert result["stale"] is False
    assert result["age_seconds"] == 0
    assert result["snapshot"]["instance"]["state"] == "running"
    assert result["snapshot"]["metrics"]["cpu_utilization"]["value"] == 12.5


def test_load_stale_snapshot(tmp_path):
    path = tmp_path / "metrics.json"
    old_time = NOW - timedelta(minutes=5)
    path.write_text(
        json.dumps(make_snapshot(old_time.isoformat())),
        encoding="utf-8",
    )

    result = load_server_metrics(path, now=NOW)

    assert result["available"] is True
    assert result["stale"] is True
    assert result["age_seconds"] == 300


def test_missing_snapshot_is_unavailable(tmp_path):
    result = load_server_metrics(
        tmp_path / "missing.json",
        now=NOW,
    )

    assert result["available"] is False
    assert result["stale"] is True
    assert result["snapshot"] is None


def test_invalid_json_is_unavailable(tmp_path):
    path = tmp_path / "metrics.json"
    path.write_text("{invalid json", encoding="utf-8")

    result = load_server_metrics(path, now=NOW)

    assert result["available"] is False
    assert result["error"] == "Metrics snapshot is invalid."


def test_invalid_timestamp_is_unavailable(tmp_path):
    path = tmp_path / "metrics.json"
    snapshot = make_snapshot("not-a-timestamp")
    path.write_text(json.dumps(snapshot), encoding="utf-8")

    result = load_server_metrics(path, now=NOW)

    assert result["available"] is False


def test_unsupported_schema_is_unavailable(tmp_path):
    path = tmp_path / "metrics.json"
    snapshot = make_snapshot()
    snapshot["schema_version"] = 99
    path.write_text(json.dumps(snapshot), encoding="utf-8")

    result = load_server_metrics(path, now=NOW)

    assert result["available"] is False
