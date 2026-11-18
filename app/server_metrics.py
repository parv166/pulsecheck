"""Read and validate the host-provided EC2 metrics snapshot."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_METRICS_FILE = "/run/pulsecheck-metrics/metrics.json"
MAX_METRICS_AGE_SECONDS = 180


def unavailable(error: str) -> dict[str, Any]:
    return {
        "available": False,
        "stale": True,
        "age_seconds": None,
        "max_age_seconds": MAX_METRICS_AGE_SECONDS,
        "snapshot": None,
        "error": error,
    }


def load_server_metrics(
    path: str | Path | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    source = Path(
        path
        if path is not None
        else os.getenv("PULSECHECK_METRICS_FILE", DEFAULT_METRICS_FILE)
    )

    try:
        raw = source.read_text(encoding="utf-8")
    except FileNotFoundError:
        return unavailable("Metrics snapshot has not been collected yet.")
    except (OSError, UnicodeError):
        return unavailable("Metrics snapshot cannot be read.")

    try:
        snapshot = json.loads(raw)

        if not isinstance(snapshot, dict):
            raise ValueError("Snapshot must be an object.")

        if snapshot.get("schema_version") != 1:
            raise ValueError("Unsupported snapshot schema.")

        collected_at = snapshot["collected_at"]
        instance = snapshot["instance"]
        metrics = snapshot["metrics"]
        collection = snapshot["collection"]

        if not isinstance(collected_at, str):
            raise ValueError("Invalid collection timestamp.")

        if not all(
            isinstance(value, dict)
            for value in (instance, metrics, collection)
        ):
            raise ValueError("Invalid snapshot structure.")

        timestamp = datetime.fromisoformat(
            collected_at.replace("Z", "+00:00")
        )

        if timestamp.tzinfo is None:
            raise ValueError("Timestamp must include a timezone.")

    except (
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return unavailable("Metrics snapshot is invalid.")

    current_time = now or datetime.now(timezone.utc)

    if current_time.tzinfo is None:
        raise ValueError("The current time must include a timezone.")

    age = (
        current_time.astimezone(timezone.utc)
        - timestamp.astimezone(timezone.utc)
    ).total_seconds()

    age_seconds = max(0, int(age))

    return {
        "available": True,
        "stale": age > MAX_METRICS_AGE_SECONDS,
        "age_seconds": age_seconds,
        "max_age_seconds": MAX_METRICS_AGE_SECONDS,
        "snapshot": snapshot,
        "error": None,
    }
