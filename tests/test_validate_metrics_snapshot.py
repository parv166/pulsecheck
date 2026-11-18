import json
import sys

import pytest

from scripts import validate_metrics_snapshot as validator


def valid_snapshot():
    return {
        "schema_version": 1,
        "collection": {"status": "ok"},
        "instance": {
            "instance_id": "i-test123",
            "state": "running",
            "system_status": "ok",
            "instance_status": "ok",
        },
        "metrics": {
            "cpu_utilization": {"value": 12.5},
            "network_in_bytes": {"value": 2048},
            "network_out_bytes": {"value": 4096},
        },
    }


def run_validator(monkeypatch, path):
    monkeypatch.setattr(
        sys,
        "argv",
        ["validate_metrics_snapshot.py", str(path), "i-test123"],
    )
    return validator.main()


def test_valid_snapshot_passes(tmp_path, monkeypatch):
    path = tmp_path / "metrics.json"
    path.write_text(json.dumps(valid_snapshot()))

    assert run_validator(monkeypatch, path) == 0


def test_wrong_instance_id_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "metrics.json"
    snapshot = valid_snapshot()
    snapshot["instance"]["instance_id"] = "i-wrong"
    path.write_text(json.dumps(snapshot))

    with pytest.raises(SystemExit, match="instance ID"):
        run_validator(monkeypatch, path)


def test_unhealthy_instance_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "metrics.json"
    snapshot = valid_snapshot()
    snapshot["instance"]["system_status"] = "impaired"
    path.write_text(json.dumps(snapshot))

    with pytest.raises(SystemExit, match="system_status"):
        run_validator(monkeypatch, path)


def test_partial_collection_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "metrics.json"
    snapshot = valid_snapshot()
    snapshot["collection"]["status"] = "partial"
    path.write_text(json.dumps(snapshot))

    with pytest.raises(SystemExit, match="incomplete"):
        run_validator(monkeypatch, path)


def test_missing_metric_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "metrics.json"
    snapshot = valid_snapshot()
    snapshot["metrics"]["network_out_bytes"]["value"] = None
    path.write_text(json.dumps(snapshot))

    with pytest.raises(SystemExit, match="network_out_bytes"):
        run_validator(monkeypatch, path)
