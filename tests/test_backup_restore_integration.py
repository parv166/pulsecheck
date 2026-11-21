import sqlite3

from scripts.backup_crypto import encrypt_file, decrypt_file
from scripts.backup_sqlite_readonly import create_backup, inspect_database


PASSPHRASE = "integration-test-passphrase-with-more-than-32-bytes"


def test_sqlite_backup_encrypt_decrypt_and_restore(tmp_path):
    source = tmp_path / "production.db"
    backup = tmp_path / "backup.db"
    encrypted = tmp_path / "backup.db.enc"
    restored = tmp_path / "restored.db"

    with sqlite3.connect(source) as db:
        db.execute(
            "CREATE TABLE monitored_targets "
            "(id INTEGER PRIMARY KEY, name TEXT)"
        )
        db.execute(
            "CREATE TABLE monitoring_history "
            "(id INTEGER PRIMARY KEY, status TEXT)"
        )
        db.execute(
            "CREATE TABLE incidents "
            "(id INTEGER PRIMARY KEY, status TEXT)"
        )
        db.execute(
            "CREATE TABLE auth_sessions "
            "(id INTEGER PRIMARY KEY, token_hash TEXT)"
        )

        db.execute(
            "INSERT INTO monitored_targets(name) VALUES (?)",
            ("example.com",),
        )
        db.executemany(
            "INSERT INTO monitoring_history(status) VALUES (?)",
            [("UP",), ("DOWN",), ("UP",)],
        )
        db.execute(
            "INSERT INTO incidents(status) VALUES (?)",
            ("RESOLVED",),
        )
        db.execute(
            "INSERT INTO auth_sessions(token_hash) VALUES (?)",
            ("test-session-hash",),
        )

    backup_metadata = create_backup(source, backup)

    encrypt_file(backup, encrypted, PASSPHRASE)

    assert encrypted.exists()
    assert not encrypted.read_bytes().startswith(b"SQLite format 3")

    restored_metadata = decrypt_file(encrypted, restored, PASSPHRASE)
    restored_database = inspect_database(restored)

    assert restored_database["integrity"] == "ok"
    assert restored_database["counts"] == {
        "monitored_targets": 1,
        "monitoring_history": 3,
        "incidents": 1,
        "auth_sessions": 1,
    }

    assert restored_metadata["sha256"] == backup_metadata["sha256"]
    assert restored_database["sha256"] == backup_metadata["sha256"]
    assert restored_metadata["size_bytes"] == backup_metadata["size_bytes"]

    assert backup.stat().st_mode & 0o777 == 0o600
    assert encrypted.stat().st_mode & 0o777 == 0o600
    assert restored.stat().st_mode & 0o777 == 0o600


def test_live_sqlite_stream_to_encrypted_artifact_and_verified_restore(tmp_path):
    import json
    import os
    import subprocess
    import sys

    source = tmp_path / "live-source.db"
    encrypted = tmp_path / "live-backup.enc"
    restored = tmp_path / "live-restored.db"

    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE monitored_targets (id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE monitoring_history (id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE incidents (id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE auth_sessions (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO monitored_targets DEFAULT VALUES")
        db.executemany(
            "INSERT INTO monitoring_history DEFAULT VALUES",
            [(), (), ()],
        )
        db.execute("INSERT INTO incidents DEFAULT VALUES")

    env = os.environ.copy()
    env["PULSECHECK_BACKUP_PASSPHRASE"] = PASSPHRASE

    producer = subprocess.Popen(
        [
            sys.executable,
            "scripts/stream_live_sqlite.py",
            "--source", str(source),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert producer.stdout is not None
    assert producer.stderr is not None

    encryptor = subprocess.Popen(
        [
            sys.executable,
            "scripts/backup_crypto.py",
            "--encrypt-stdin",
            "--destination", str(encrypted),
        ],
        stdin=producer.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    producer.stdout.close()
    encryption_stdout, encryption_stderr = encryptor.communicate()
    stream_metadata_raw = producer.stderr.read()
    producer_status = producer.wait()

    assert producer_status == 0, stream_metadata_raw.decode(errors="replace")
    assert encryptor.returncode == 0, encryption_stderr.decode(errors="replace")

    stream_metadata = json.loads(stream_metadata_raw)
    encryption_metadata = json.loads(encryption_stdout)

    assert encrypted.exists()
    assert encrypted.stat().st_size == encryption_metadata["size_bytes"]

    decrypt_file(encrypted, restored, PASSPHRASE)
    restored_metadata = inspect_database(restored)

    assert restored_metadata["integrity"] == "ok"
    assert restored_metadata["sha256"] == stream_metadata["sha256"]
    assert restored_metadata["size_bytes"] == stream_metadata["size_bytes"]
    assert restored_metadata["counts"] == stream_metadata["counts"]
    assert encrypted.stat().st_mode & 0o777 == 0o600
    assert restored.stat().st_mode & 0o777 == 0o600
