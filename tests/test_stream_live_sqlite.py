import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from scripts.backup_sqlite_readonly import inspect_database


SCRIPT = Path("scripts/stream_live_sqlite.py")


def create_database(path):
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE monitored_targets (id INTEGER PRIMARY KEY)"
        )
        db.execute(
            "CREATE TABLE monitoring_history (id INTEGER PRIMARY KEY)"
        )
        db.execute(
            "CREATE TABLE incidents (id INTEGER PRIMARY KEY)"
        )
        db.execute(
            "CREATE TABLE auth_sessions (id INTEGER PRIMARY KEY)"
        )
        db.execute("INSERT INTO monitored_targets DEFAULT VALUES")
        db.execute("INSERT INTO monitoring_history DEFAULT VALUES")
        db.execute("INSERT INTO incidents DEFAULT VALUES")


def test_live_stream_outputs_valid_sqlite_and_separate_metadata(tmp_path):
    source = tmp_path / "source.db"
    received = tmp_path / "received.db"
    create_database(source)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--source",
            str(source),
        ],
        capture_output=True,
        check=True,
    )

    assert result.stdout.startswith(b"SQLite format 3")
    received.write_bytes(result.stdout)

    metadata = json.loads(result.stderr)
    inspected = inspect_database(received)

    assert metadata == inspected
    assert metadata["integrity"] == "ok"
    assert metadata["counts"] == {
        "monitored_targets": 1,
        "monitoring_history": 1,
        "incidents": 1,
        "auth_sessions": 0,
    }
    assert metadata["sha256"] == hashlib.sha256(result.stdout).hexdigest()


def test_live_stream_captures_committed_wal_data(tmp_path):
    source = tmp_path / "wal-source.db"

    db = sqlite3.connect(source)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(
            "CREATE TABLE monitored_targets (id INTEGER PRIMARY KEY)"
        )
        db.execute(
            "CREATE TABLE monitoring_history (id INTEGER PRIMARY KEY)"
        )
        db.execute(
            "CREATE TABLE incidents (id INTEGER PRIMARY KEY)"
        )
        db.execute(
            "CREATE TABLE auth_sessions (id INTEGER PRIMARY KEY)"
        )
        db.execute("INSERT INTO monitored_targets DEFAULT VALUES")
        db.commit()

        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--source",
                str(source),
            ],
            capture_output=True,
            check=True,
        )
    finally:
        db.close()

    metadata = json.loads(result.stderr)
    assert metadata["integrity"] == "ok"
    assert metadata["counts"]["monitored_targets"] == 1


def test_live_stream_rejects_incomplete_database_without_output(tmp_path):
    source = tmp_path / "incomplete.db"

    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE monitored_targets (id INTEGER PRIMARY KEY)")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--source",
            str(source),
        ],
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == b""


def test_live_stream_rejects_symlink(tmp_path):
    source = tmp_path / "source.db"
    link = tmp_path / "link.db"
    create_database(source)
    link.symlink_to(source)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--source",
            str(link),
        ],
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == b""
