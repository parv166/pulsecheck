#!/usr/bin/env python3
"""Create a SQLite backup and stream only its database bytes to stdout."""

import argparse
import json
import shutil
import sys
from pathlib import Path

from backup_sqlite_readonly import create_backup


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()

    metadata = create_backup(args.source, args.destination)

    try:
        # Metadata must never enter the binary database stream.
        sys.stderr.write(json.dumps(metadata, sort_keys=True) + "\n")
        sys.stderr.flush()

        with args.destination.open("rb") as backup:
            shutil.copyfileobj(
                backup,
                sys.stdout.buffer,
                length=1024 * 1024,
            )

        sys.stdout.buffer.flush()
    finally:
        args.destination.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
