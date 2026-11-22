#!/usr/bin/env python3
"""Collect read-only EC2 and CloudWatch metrics into an atomic JSON snapshot."""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


REGION = os.getenv("AWS_REGION", "ap-south-1")
INSTANCE_ID = os.getenv("PULSECHECK_EC2_INSTANCE_ID", "").strip()
SNAPSHOT_PATH = Path(
    os.getenv(
        "PULSECHECK_METRICS_FILE",
        "/var/lib/pulsecheck-metrics/metrics.json",
    )
)
PERIOD_SECONDS = 300


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def aws_json(*args):
    """Run AWS CLI without exposing credentials or command errors."""
    result = subprocess.run(
        ["aws", *args, "--region", REGION, "--output", "json"],
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"AWS CLI command failed with exit code {result.returncode}"
        )

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("AWS CLI returned invalid JSON") from exc


def latest_metric(results, metric_id, unit, statistic):
    for result in results:
        if result.get("Id") != metric_id:
            continue

        timestamps = result.get("Timestamps", [])
        values = result.get("Values", [])

        if not timestamps or not values or len(timestamps) != len(values):
            return {
                "value": None,
                "unit": unit,
                "statistic": statistic,
                "period_seconds": PERIOD_SECONDS,
                "timestamp": None,
            }

        timestamp, value = max(
            zip(timestamps, values),
            key=lambda item: item[0],
        )

        return {
            "value": float(value),
            "unit": unit,
            "statistic": statistic,
            "period_seconds": PERIOD_SECONDS,
            "timestamp": timestamp,
        }

    return {
        "value": None,
        "unit": unit,
        "statistic": statistic,
        "period_seconds": PERIOD_SECONDS,
        "timestamp": None,
    }


def collect():
    errors = []
    instance = {
        "instance_id": INSTANCE_ID,
        "state": "unknown",
        "instance_type": None,
        "availability_zone": None,
        "launch_time": None,
        "system_status": "unknown",
        "instance_status": "unknown",
    }

    metrics = {
        "cpu_utilization": latest_metric([], "cpu", "Percent", "Average"),
        "network_in_bytes": latest_metric([], "network_in", "Bytes", "Sum"),
        "network_out_bytes": latest_metric([], "network_out", "Bytes", "Sum"),
    }

    started_at = utc_now()

    # EC2 instance information.
    try:
        response = aws_json(
            "ec2",
            "describe-instances",
            "--instance-ids",
            INSTANCE_ID,
        )

        reservations = response.get("Reservations", [])
        instances = [
            item
            for reservation in reservations
            for item in reservation.get("Instances", [])
        ]

        if not instances:
            raise RuntimeError("Configured EC2 instance was not found")

        item = instances[0]
        instance.update({
            "instance_id": item.get("InstanceId"),
            "state": item.get("State", {}).get("Name", "unknown"),
            "instance_type": item.get("InstanceType"),
            "availability_zone": item.get("Placement", {}).get(
                "AvailabilityZone"
            ),
            "launch_time": item.get("LaunchTime"),
        })

    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        errors.append(f"ec2_describe: {exc}")

    # EC2 system and instance status checks.
    try:
        response = aws_json(
            "ec2",
            "describe-instance-status",
            "--instance-ids",
            INSTANCE_ID,
            "--include-all-instances",
        )

        statuses = response.get("InstanceStatuses", [])

        if statuses:
            item = statuses[0]
            instance["system_status"] = item.get(
                "SystemStatus", {}
            ).get("Status", "unknown")
            instance["instance_status"] = item.get(
                "InstanceStatus", {}
            ).get("Status", "unknown")

    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        errors.append(f"ec2_status: {exc}")

    # CloudWatch metrics over the last 30 minutes.
    try:
        end_time = started_at
        start_time = end_time - timedelta(minutes=30)

        queries = [
            {
                "Id": "cpu",
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/EC2",
                        "MetricName": "CPUUtilization",
                        "Dimensions": [
                            {"Name": "InstanceId", "Value": INSTANCE_ID}
                        ],
                    },
                    "Period": PERIOD_SECONDS,
                    "Stat": "Average",
                },
                "ReturnData": True,
            },
            {
                "Id": "network_in",
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/EC2",
                        "MetricName": "NetworkIn",
                        "Dimensions": [
                            {"Name": "InstanceId", "Value": INSTANCE_ID}
                        ],
                    },
                    "Period": PERIOD_SECONDS,
                    "Stat": "Sum",
                },
                "ReturnData": True,
            },
            {
                "Id": "network_out",
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/EC2",
                        "MetricName": "NetworkOut",
                        "Dimensions": [
                            {"Name": "InstanceId", "Value": INSTANCE_ID}
                        ],
                    },
                    "Period": PERIOD_SECONDS,
                    "Stat": "Sum",
                },
                "ReturnData": True,
            },
        ]

        response = aws_json(
            "cloudwatch",
            "get-metric-data",
            "--start-time",
            start_time.isoformat(),
            "--end-time",
            end_time.isoformat(),
            "--metric-data-queries",
            json.dumps(queries),
        )

        results = response.get("MetricDataResults", [])

        metrics = {
            "cpu_utilization": latest_metric(
                results, "cpu", "Percent", "Average"
            ),
            "network_in_bytes": latest_metric(
                results, "network_in", "Bytes", "Sum"
            ),
            "network_out_bytes": latest_metric(
                results, "network_out", "Bytes", "Sum"
            ),
        }

    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        errors.append(f"cloudwatch: {exc}")

    available_metrics = sum(
        metric["value"] is not None
        for metric in metrics.values()
    )

    instance_healthy = (
        instance["state"] == "running"
        and instance["system_status"] == "ok"
        and instance["instance_status"] == "ok"
    )

    if errors or available_metrics != 3 or not instance_healthy:
        status = (
            "partial"
            if available_metrics or instance["state"] != "unknown"
            else "error"
        )
    else:
        status = "ok"

    return {
        "schema_version": 1,
        "collected_at": utc_now().isoformat(),
        "region": REGION,
        "instance": instance,
        "metrics": metrics,
        "collection": {
            "status": status,
            "errors": errors,
        },
    }


def write_snapshot(snapshot):
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)

    temporary_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=SNAPSHOT_PATH.parent,
            prefix=".metrics-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(snapshot, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, SNAPSHOT_PATH)

    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def main():
    if not INSTANCE_ID:
        print(
            "ERROR: PULSECHECK_EC2_INSTANCE_ID is not configured.",
            file=sys.stderr,
        )
        return 2

    try:
        snapshot = collect()
        write_snapshot(snapshot)

        print(f"Collection status: {snapshot['collection']['status']}")
        print(f"Available metrics: {sum(m['value'] is not None for m in snapshot['metrics'].values())}/3")
        print(f"Snapshot: {SNAPSHOT_PATH}")

        if snapshot["collection"]["status"] != "ok":
            print("ERROR: Collection is incomplete; the timer will retry.")
            return 1

        return 0

    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"ERROR: Could not write metrics snapshot: {type(exc).__name__}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
