#!/usr/bin/env python3
"""Create a consistent in-memory SQLite snapshot and stream its bytes."""

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path


TABLES = (
    "monitored_targets",
    "monitoring_history",
    "incidents",
    "auth_sessions",
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("/app/data/pulsecheck.db"),
    )
    args = parser.parse_args()

    source_path = args.source

    if source_path.is_symlink() or not source_path.is_file():
        raise ValueError("Source must be an existing regular database file.")

    source = None
    snapshot = None

    try:
        uri = source_path.resolve().as_uri() + "?mode=ro"
        source = sqlite3.connect(uri, uri=True, timeout=30)
        snapshot = sqlite3.connect(":memory:", timeout=30)

        # SQLite online backup API provides a consistent point-in-time copy.
        source.backup(snapshot, pages=256, sleep=0.1)

        integrity = snapshot.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0]

        if integrity != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {integrity}")

        existing = {
            row[0]
            for row in snapshot.execute(
                "SELECT name FROM sqlite_master WHERE type = ?",
                ("table",),
            )
        }

        missing = sorted(set(TABLES) - existing)
        if missing:
            raise RuntimeError(
                "Required tables are missing: " + ", ".join(missing)
            )

        counts = {
            table: snapshot.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0]
            for table in TABLES
        }

        # serialize() returns a standalone SQLite database image.
        database_bytes = snapshot.serialize()

    finally:
        if snapshot is not None:
            snapshot.close()
        if source is not None:
            source.close()

    metadata = {
        "integrity": integrity,
        "counts": counts,
        "sha256": hashlib.sha256(database_bytes).hexdigest(),
        "size_bytes": len(database_bytes),
    }

    # Keep diagnostics separate from the binary database stream.
    sys.stderr.write(json.dumps(metadata, sort_keys=True) + "\n")
    sys.stderr.flush()

    sys.stdout.buffer.write(database_bytes)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
