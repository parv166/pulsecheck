import json
from types import SimpleNamespace

from scripts import collect_ec2_metrics as collector


INSTANCE_RESPONSE = {
    "Reservations": [{
        "Instances": [{
            "InstanceId": "i-test123",
            "InstanceType": "t3.small",
            "State": {"Name": "running"},
            "Placement": {"AvailabilityZone": "ap-south-1a"},
            "LaunchTime": "2026-01-01T00:00:00+00:00",
        }]
    }]
}

STATUS_RESPONSE = {
    "InstanceStatuses": [{
        "SystemStatus": {"Status": "ok"},
        "InstanceStatus": {"Status": "ok"},
    }]
}


def metric_response():
    timestamp = "2026-11-19T00:00:00+00:00"

    return {
        "MetricDataResults": [
            {"Id": "cpu", "Timestamps": [timestamp], "Values": [12.5]},
            {"Id": "network_in", "Timestamps": [timestamp], "Values": [2048]},
            {"Id": "network_out", "Timestamps": [timestamp], "Values": [4096]},
        ]
    }


def mock_aws_success(command, **kwargs):
    action = tuple(command[1:3])

    responses = {
        ("ec2", "describe-instances"): INSTANCE_RESPONSE,
        ("ec2", "describe-instance-status"): STATUS_RESPONSE,
        ("cloudwatch", "get-metric-data"): metric_response(),
    }

    response = responses[action]

    return SimpleNamespace(
        returncode=0,
        stdout=json.dumps(response),
        stderr="",
    )


def test_collect_returns_instance_and_cloudwatch_metrics(monkeypatch):
    monkeypatch.setattr(collector, "INSTANCE_ID", "i-test123")
    monkeypatch.setattr(collector.subprocess, "run", mock_aws_success)

    snapshot = collector.collect()

    assert snapshot["collection"]["status"] == "ok"
    assert snapshot["instance"]["state"] == "running"
    assert snapshot["instance"]["instance_type"] == "t3.small"
    assert snapshot["instance"]["system_status"] == "ok"
    assert snapshot["instance"]["instance_status"] == "ok"

    assert snapshot["metrics"]["cpu_utilization"]["value"] == 12.5
    assert snapshot["metrics"]["network_in_bytes"]["value"] == 2048
    assert snapshot["metrics"]["network_out_bytes"]["value"] == 4096


def test_collect_marks_missing_cloudwatch_data_as_partial(monkeypatch):
    monkeypatch.setattr(collector, "INSTANCE_ID", "i-test123")

    def mock_partial(command, **kwargs):
        if command[1:3] == ["cloudwatch", "get-metric-data"]:
            response = {
                "MetricDataResults": [
                    {
                        "Id": "cpu",
                        "Timestamps": ["2026-11-19T00:00:00+00:00"],
                        "Values": [8.0],
                    }
                ]
            }
        else:
            responses = {
                ("ec2", "describe-instances"): INSTANCE_RESPONSE,
                ("ec2", "describe-instance-status"): STATUS_RESPONSE,
            }
            response = responses[tuple(command[1:3])]

        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(response),
            stderr="",
        )

    monkeypatch.setattr(collector.subprocess, "run", mock_partial)

    snapshot = collector.collect()

    assert snapshot["collection"]["status"] == "partial"
    assert snapshot["metrics"]["cpu_utilization"]["value"] == 8.0
    assert snapshot["metrics"]["network_in_bytes"]["value"] is None
    assert snapshot["metrics"]["network_out_bytes"]["value"] is None


def test_collect_records_cloudwatch_failure_without_exposing_stderr(monkeypatch):
    monkeypatch.setattr(collector, "INSTANCE_ID", "i-test123")

    def mock_failure(command, **kwargs):
        if command[1:3] == ["cloudwatch", "get-metric-data"]:
            return SimpleNamespace(
                returncode=254,
                stdout="",
                stderr="Sensitive diagnostic text",
            )

        responses = {
            ("ec2", "describe-instances"): INSTANCE_RESPONSE,
            ("ec2", "describe-instance-status"): STATUS_RESPONSE,
        }

        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(responses[tuple(command[1:3])]),
            stderr="",
        )

    monkeypatch.setattr(collector.subprocess, "run", mock_failure)

    snapshot = collector.collect()

    assert snapshot["collection"]["status"] == "partial"
    assert any(
        error.startswith("cloudwatch:")
        for error in snapshot["collection"]["errors"]
    )

    assert "Sensitive diagnostic text" not in json.dumps(snapshot)


def test_write_snapshot_is_atomic_and_readable(tmp_path, monkeypatch):
    destination = tmp_path / "metrics.json"
    monkeypatch.setattr(collector, "SNAPSHOT_PATH", destination)

    snapshot = {
        "schema_version": 1,
        "collection": {"status": "ok", "errors": []},
    }

    collector.write_snapshot(snapshot)

    assert destination.is_file()
    assert json.loads(destination.read_text()) == snapshot
    assert destination.stat().st_mode & 0o777 == 0o644
    assert list(tmp_path.glob(".metrics-*.tmp")) == []


def test_latest_metric_selects_newest_timestamp():
    results = [{
        "Id": "cpu",
        "Timestamps": [
            "2026-11-19T00:00:00+00:00",
            "2026-11-19T00:05:00+00:00",
        ],
        "Values": [5.0, 15.0],
    }]

    metric = collector.latest_metric(
        results,
        "cpu",
        "Percent",
        "Average",
    )

    assert metric["value"] == 15.0
    assert metric["timestamp"] == "2026-11-19T00:05:00+00:00"


def test_collect_marks_missing_ec2_status_checks_as_partial(monkeypatch):
    monkeypatch.setattr(collector, "INSTANCE_ID", "i-test123")

    def mock_missing_status(command, **kwargs):
        action = tuple(command[1:3])

        responses = {
            ("ec2", "describe-instances"): INSTANCE_RESPONSE,
            ("ec2", "describe-instance-status"): {
                "InstanceStatuses": []
            },
            ("cloudwatch", "get-metric-data"): metric_response(),
        }

        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(responses[action]),
            stderr="",
        )

    monkeypatch.setattr(
        collector.subprocess,
        "run",
        mock_missing_status,
    )

    snapshot = collector.collect()

    assert snapshot["instance"]["state"] == "running"
    assert snapshot["instance"]["system_status"] == "unknown"
    assert snapshot["collection"]["status"] == "partial"
