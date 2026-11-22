#!/usr/bin/env python3
"""Create or inspect a consistent SQLite backup without modifying its source."""

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path

TABLES = (
    "monitored_targets",
    "monitoring_history",
    "incidents",
    "auth_sessions",
)


def inspect_database(path: Path) -> dict:
    path = Path(path)

    if path.is_symlink() or not path.is_file():
        raise ValueError("Database must be an existing regular file, not a symlink.")

    uri = path.resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=30)

    try:
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {integrity}")

        existing = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = ?",
                ("table",),
            )
        }

        missing = sorted(set(TABLES) - existing)
        if missing:
            raise RuntimeError(
                "Required database tables are missing: " + ", ".join(missing)
            )

        counts = {
            table: db.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0]
            for table in TABLES
        }
    finally:
        db.close()

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)

    return {
        "integrity": integrity,
        "counts": counts,
        "sha256": digest.hexdigest(),
        "size_bytes": path.stat().st_size,
    }


def create_backup(source_path: Path, destination_path: Path) -> dict:
    source_path = Path(source_path)
    destination_path = Path(destination_path)

    if source_path.is_symlink() or not source_path.is_file():
        raise ValueError("Source database is missing or is not a regular file.")

    if not destination_path.parent.is_dir():
        raise NotADirectoryError("Backup destination directory does not exist.")

    if destination_path.exists() or destination_path.is_symlink():
        raise FileExistsError("Refusing to overwrite an existing backup.")

    if source_path.resolve() == destination_path.resolve():
        raise ValueError("Source and destination must be different.")

    # mkstemp creates the temporary file with restrictive 0600 permissions.
    # Keeping it in the destination directory ensures the final hard link
    # is on the same filesystem.
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination_path.name}.",
        suffix=".tmp",
        dir=destination_path.parent,
    )
    os.close(file_descriptor)

    temporary_path = Path(temporary_name)
    source = None
    destination = None

    try:
        source_uri = source_path.resolve().as_uri() + "?mode=ro"
        source = sqlite3.connect(source_uri, uri=True, timeout=30)
        destination = sqlite3.connect(str(temporary_path), timeout=30)

        source.backup(destination, pages=256, sleep=0.1)

        destination.close()
        destination = None

        source.close()
        source = None

        # Validate the completed backup before making it visible at the
        # requested destination.
        result = inspect_database(temporary_path)

        # Atomic publication: fails if destination already exists.
        # Unlike os.replace(), os.link() does not overwrite the destination.
        os.link(temporary_path, destination_path)

        return result

    finally:
        if destination is not None:
            destination.close()

        if source is not None:
            source.close()

        temporary_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--inspect", type=Path)
    args = parser.parse_args()

    if args.inspect is not None:
        if args.source is not None or args.destination is not None:
            parser.error("--inspect cannot be combined with --source or --destination")
        result = inspect_database(args.inspect)

    elif args.source is not None and args.destination is not None:
        result = create_backup(args.source, args.destination)

    else:
        parser.error("Provide --inspect PATH or both --source and --destination")

    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
