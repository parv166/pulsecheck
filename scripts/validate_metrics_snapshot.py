#!/usr/bin/env python3
"""Validate the first EC2 metrics snapshot before deployment."""

import json
import math
import sys
from pathlib import Path


def main():
    if len(sys.argv) != 3:
        print("Usage: validate_metrics_snapshot.py SNAPSHOT EXPECTED_INSTANCE_ID")
        return 2

    path = Path(sys.argv[1])
    expected_instance_id = sys.argv[2]

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: Cannot read metrics snapshot: {type(exc).__name__}")
        return 1

    if data.get("schema_version") != 1:
        raise SystemExit("ERROR: Unsupported snapshot schema.")

    if data.get("collection", {}).get("status") != "ok":
        raise SystemExit("ERROR: Initial AWS collection is incomplete.")

    instance = data.get("instance", {})

    if instance.get("instance_id") != expected_instance_id:
        raise SystemExit("ERROR: Snapshot instance ID does not match EC2 host.")

    if instance.get("state") != "running":
        raise SystemExit("ERROR: EC2 instance is not running.")

    for name in ("system_status", "instance_status"):
        if instance.get(name) != "ok":
            raise SystemExit(f"ERROR: EC2 {name} is not healthy.")

    metrics = data.get("metrics", {})

    for name in (
        "cpu_utilization",
        "network_in_bytes",
        "network_out_bytes",
    ):
        metric = metrics.get(name, {})
        value = metric.get("value")

        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise SystemExit(f"ERROR: Missing or invalid metric: {name}")

    print("PASS: Correct EC2 instance discovered.")
    print("PASS: EC2 system and instance checks are healthy.")
    print("PASS: CPU, NetworkIn and NetworkOut metrics are present.")
    print("Snapshot permissions:", oct(path.stat().st_mode & 0o777))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
