import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from scripts.backup_sqlite_readonly import inspect_database


SCRIPT = Path("scripts/stream_sqlite_backup.py")


def create_database(path):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE monitored_targets (id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE monitoring_history (id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE incidents (id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE auth_sessions (id INTEGER PRIMARY KEY)")

        db.execute("INSERT INTO monitored_targets DEFAULT VALUES")
        db.execute("INSERT INTO monitoring_history DEFAULT VALUES")


def test_stream_contains_only_database_bytes_and_metadata_is_stderr(tmp_path):
    source = tmp_path / "source.db"
    temporary_backup = tmp_path / "temporary.db"
    received = tmp_path / "received.db"

    create_database(source)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--source", str(source),
            "--destination", str(temporary_backup),
        ],
        capture_output=True,
        check=True,
    )

    assert result.stdout.startswith(b"SQLite format 3")
    assert b"{" not in result.stdout[:16]
    assert not temporary_backup.exists()

    received.write_bytes(result.stdout)

    metadata = json.loads(result.stderr)
    assert metadata == inspect_database(received)
    assert metadata["counts"] == {
        "monitored_targets": 1,
        "monitoring_history": 1,
        "incidents": 0,
        "auth_sessions": 0,
    }


def test_stream_rejects_missing_source_without_database_output(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--source", str(tmp_path / "missing.db"),
            "--destination", str(tmp_path / "temporary.db"),
        ],
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == b""
    assert not (tmp_path / "temporary.db").exists()

def test_stream_refuses_to_overwrite_existing_destination(tmp_path):
    source = tmp_path / "source.db"
    destination = tmp_path / "temporary.db"

    create_database(source)
    destination.write_bytes(b"existing backup")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--source", str(source),
            "--destination", str(destination),
        ],
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == b""
    assert destination.read_bytes() == b"existing backup"
    assert list(tmp_path.glob(".temporary.db.*.tmp")) == []
