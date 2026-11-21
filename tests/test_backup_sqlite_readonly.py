import sqlite3

import pytest

from scripts.backup_sqlite_readonly import (
    TABLES,
    create_backup,
    inspect_database,
)


def create_sample_database(path):
    with sqlite3.connect(path) as db:
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

        db.execute("INSERT INTO monitored_targets(name) VALUES ('example')")
        db.execute("INSERT INTO monitoring_history(status) VALUES ('UP')")
        db.execute("INSERT INTO monitoring_history(status) VALUES ('DOWN')")
        db.execute("INSERT INTO incidents(status) VALUES ('RESOLVED')")
        db.execute("INSERT INTO auth_sessions(token_hash) VALUES ('hash')")


def test_online_backup_preserves_all_table_counts_and_integrity(tmp_path):
    source = tmp_path / "source.db"
    destination = tmp_path / "backup.db"
    create_sample_database(source)

    result = create_backup(source, destination)

    assert result["integrity"] == "ok"
    assert result["counts"] == {
        "monitored_targets": 1,
        "monitoring_history": 2,
        "incidents": 1,
        "auth_sessions": 1,
    }
    assert result == inspect_database(destination)
    assert destination.stat().st_mode & 0o777 == 0o600


def test_backup_does_not_overwrite_existing_destination(tmp_path):
    source = tmp_path / "source.db"
    destination = tmp_path / "backup.db"
    create_sample_database(source)
    destination.write_bytes(b"existing backup")

    with pytest.raises(FileExistsError):
        create_backup(source, destination)

    assert destination.read_bytes() == b"existing backup"


def test_backup_rejects_missing_source(tmp_path):
    with pytest.raises(ValueError):
        create_backup(tmp_path / "missing.db", tmp_path / "backup.db")


def test_inspection_rejects_missing_required_tables(tmp_path):
    path = tmp_path / "incomplete.db"

    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE monitored_targets (id INTEGER)")

    with pytest.raises(RuntimeError, match="Required database tables"):
        inspect_database(path)


def test_inspection_rejects_symlink(tmp_path):
    source = tmp_path / "source.db"
    link = tmp_path / "link.db"
    create_sample_database(source)
    link.symlink_to(source)

    with pytest.raises(ValueError, match="symlink"):
        inspect_database(link)


def test_failed_validation_removes_temporary_backup(tmp_path, monkeypatch):
    from scripts import backup_sqlite_readonly

    source = tmp_path / "source.db"
    destination = tmp_path / "backup.db"
    create_sample_database(source)

    def fail_validation(_path):
        raise RuntimeError("simulated validation failure")

    monkeypatch.setattr(
        backup_sqlite_readonly,
        "inspect_database",
        fail_validation,
    )

    with pytest.raises(RuntimeError, match="simulated validation failure"):
        create_backup(source, destination)

    assert not destination.exists()
    assert list(tmp_path.glob(".backup.db.*.tmp")) == []


def test_destination_created_during_backup_is_never_overwritten(
    tmp_path, monkeypatch
):
    import os
    from scripts import backup_sqlite_readonly

    source = tmp_path / "source.db"
    destination = tmp_path / "backup.db"
    create_sample_database(source)

    real_link = os.link

    def create_destination_then_link(source_name, destination_name, **kwargs):
        destination.write_bytes(b"created by another process")
        return real_link(source_name, destination_name, **kwargs)

    monkeypatch.setattr(
        backup_sqlite_readonly.os,
        "link",
        create_destination_then_link,
    )

    with pytest.raises(FileExistsError):
        create_backup(source, destination)

    assert destination.read_bytes() == b"created by another process"
    assert list(tmp_path.glob(".backup.db.*.tmp")) == []
