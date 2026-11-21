#!/usr/bin/env python3

import argparse
import json
import os
import sqlite3
from pathlib import Path


TABLES = (
    "monitored_targets",
    "monitoring_history",
    "incidents",
    "auth_sessions",
)


def backup_database(
    source_dir: Path,
    destination_dir: Path,
    uid: int = 65532,
    gid: int = 65532,
) -> dict:
    source_path = source_dir / "pulsecheck.db"
    destination_path = destination_dir / "pulsecheck.db"

    if not source_path.is_file():
        raise FileNotFoundError("Original production SQLite database is missing.")

    if source_path.is_symlink():
        raise ValueError("Source database must not be a symbolic link.")

    if not destination_dir.is_dir():
        raise NotADirectoryError("Destination volume directory is missing.")

    if destination_path.exists():
        raise FileExistsError("Destination database already exists.")

    if source_dir.resolve() == destination_dir.resolve():
        raise ValueError("Source and destination directories must be different.")

    source = sqlite3.connect(str(source_path), timeout=30)
    destination = sqlite3.connect(str(destination_path), timeout=30)

    try:
        source.backup(destination, pages=256, sleep=0.1)

        integrity = destination.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0]

        if integrity != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {integrity}")

        counts = {}

        for table in TABLES:
            exists = destination.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()

            if exists is None:
                raise RuntimeError(f"Required database table is missing: {table}")

            counts[table] = destination.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0]

    finally:
        destination.close()
        source.close()

    # Set the application runtime's required ownership and permissions.
    for entry in destination_dir.iterdir():
        if entry.is_file() and not entry.is_symlink():
            os.chown(entry, uid, gid)
            os.chmod(entry, 0o640)

    os.chown(destination_dir, uid, gid)
    os.chmod(destination_dir, 0o750)

    database_stat = destination_path.stat()
    directory_stat = destination_dir.stat()

    if (database_stat.st_uid, database_stat.st_gid) != (uid, gid):
        raise RuntimeError("Destination database ownership verification failed.")

    if (directory_stat.st_uid, directory_stat.st_gid) != (uid, gid):
        raise RuntimeError("Destination directory ownership verification failed.")

    if database_stat.st_mode & 0o777 != 0o640:
        raise RuntimeError("Destination database permission verification failed.")

    if directory_stat.st_mode & 0o777 != 0o750:
        raise RuntimeError("Destination directory permission verification failed.")

    return {
        "integrity": integrity,
        "counts": counts,
        "database_owner": f"{uid}:{gid}",
        "database_mode": "640",
        "directory_owner": f"{uid}:{gid}",
        "directory_mode": "750",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create and validate a consistent PulseCheck SQLite volume backup."
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--uid", type=int, default=65532)
    parser.add_argument("--gid", type=int, default=65532)

    args = parser.parse_args()

    result = backup_database(
        args.source,
        args.destination,
        uid=args.uid,
        gid=args.gid,
    )

    print("SQLite backup completed successfully.")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
