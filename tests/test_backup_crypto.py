import hashlib
import os

import pytest

from scripts.backup_crypto import (
    MAGIC,
    SALT_LENGTH,
    NONCE_LENGTH,
    encrypt_file,
    decrypt_file,
)


PASSPHRASE = "test-only-passphrase-with-at-least-32-bytes"
WRONG_PASSPHRASE = "different-test-passphrase-with-32-bytes"


def temporary_files(directory):
    return list(directory.glob(".*.tmp"))


def test_encryption_and_decryption_round_trip(tmp_path):
    source = tmp_path / "backup.db"
    encrypted = tmp_path / "backup.db.enc"
    restored = tmp_path / "restored.db"

    content = os.urandom(2 * 1024 * 1024 + 123)
    source.write_bytes(content)

    encrypted_metadata = encrypt_file(source, encrypted, PASSPHRASE)

    assert encrypted.exists()
    assert encrypted.stat().st_mode & 0o777 == 0o600
    assert encrypted.read_bytes() != content
    assert content[:64] not in encrypted.read_bytes()

    restored_metadata = decrypt_file(encrypted, restored, PASSPHRASE)

    assert restored.read_bytes() == content
    assert restored.stat().st_mode & 0o777 == 0o600
    assert encrypted_metadata["sha256"] == hashlib.sha256(
        encrypted.read_bytes()
    ).hexdigest()
    assert restored_metadata["sha256"] == hashlib.sha256(content).hexdigest()
    assert restored_metadata["size_bytes"] == len(content)
    assert temporary_files(tmp_path) == []


def test_wrong_passphrase_does_not_publish_plaintext(tmp_path):
    source = tmp_path / "backup.db"
    encrypted = tmp_path / "backup.db.enc"
    restored = tmp_path / "restored.db"

    source.write_bytes(b"private database content")
    encrypt_file(source, encrypted, PASSPHRASE)

    with pytest.raises(ValueError, match="authentication failed"):
        decrypt_file(encrypted, restored, WRONG_PASSPHRASE)

    assert not restored.exists()
    assert temporary_files(tmp_path) == []


def test_modified_ciphertext_is_rejected(tmp_path):
    source = tmp_path / "backup.db"
    encrypted = tmp_path / "backup.db.enc"
    restored = tmp_path / "restored.db"

    source.write_bytes(os.urandom(4096))
    encrypt_file(source, encrypted, PASSPHRASE)

    damaged = bytearray(encrypted.read_bytes())
    ciphertext_offset = len(MAGIC) + SALT_LENGTH + NONCE_LENGTH
    damaged[ciphertext_offset + 10] ^= 0x01
    encrypted.write_bytes(damaged)

    with pytest.raises(ValueError, match="authentication failed"):
        decrypt_file(encrypted, restored, PASSPHRASE)

    assert not restored.exists()
    assert temporary_files(tmp_path) == []


def test_encryption_does_not_overwrite_existing_destination(tmp_path):
    source = tmp_path / "backup.db"
    encrypted = tmp_path / "backup.db.enc"

    source.write_bytes(b"database")
    encrypted.write_bytes(b"existing artifact")

    with pytest.raises(FileExistsError):
        encrypt_file(source, encrypted, PASSPHRASE)

    assert encrypted.read_bytes() == b"existing artifact"


def test_decryption_does_not_overwrite_existing_destination(tmp_path):
    source = tmp_path / "backup.db"
    encrypted = tmp_path / "backup.db.enc"
    restored = tmp_path / "restored.db"

    source.write_bytes(b"database")
    encrypt_file(source, encrypted, PASSPHRASE)
    restored.write_bytes(b"existing file")

    with pytest.raises(FileExistsError):
        decrypt_file(encrypted, restored, PASSPHRASE)

    assert restored.read_bytes() == b"existing file"


def test_short_passphrase_is_rejected(tmp_path):
    source = tmp_path / "backup.db"
    encrypted = tmp_path / "backup.db.enc"

    source.write_bytes(b"database")

    with pytest.raises(ValueError, match="at least 32"):
        encrypt_file(source, encrypted, "too-short")

    assert not encrypted.exists()
    assert temporary_files(tmp_path) == []


def test_random_salt_and_nonce_produce_different_ciphertext(tmp_path):
    source = tmp_path / "backup.db"
    encrypted_one = tmp_path / "one.enc"
    encrypted_two = tmp_path / "two.enc"

    source.write_bytes(b"same database content")

    encrypt_file(source, encrypted_one, PASSPHRASE)
    encrypt_file(source, encrypted_two, PASSPHRASE)

    assert encrypted_one.read_bytes() != encrypted_two.read_bytes()


def test_invalid_encrypted_header_is_rejected_and_cleaned(tmp_path):
    encrypted = tmp_path / "invalid.enc"
    restored = tmp_path / "restored.db"

    encrypted.write_bytes(b"invalid-format")

    with pytest.raises(ValueError, match="truncated|invalid"):
        decrypt_file(encrypted, restored, PASSPHRASE)

    assert not restored.exists()
    assert temporary_files(tmp_path) == []


def test_stream_encryption_round_trip(tmp_path):
    from io import BytesIO
    from scripts.backup_crypto import encrypt_stream

    encrypted = tmp_path / "stream.enc"
    restored = tmp_path / "stream-restored.db"
    content = os.urandom(3 * 1024 * 1024 + 17)

    metadata = encrypt_stream(
        BytesIO(content),
        encrypted,
        PASSPHRASE,
    )

    assert encrypted.exists()
    assert metadata["size_bytes"] == encrypted.stat().st_size
    assert encrypted.stat().st_mode & 0o777 == 0o600

    decrypt_file(encrypted, restored, PASSPHRASE)

    assert restored.read_bytes() == content
    assert temporary_files(tmp_path) == []


def test_encrypt_stdin_cli_reads_binary_input_and_creates_artifact(tmp_path):
    import json
    import subprocess
    import sys

    source_content = os.urandom(1024 * 1024 + 31)
    encrypted = tmp_path / "stdin-backup.enc"
    restored = tmp_path / "stdin-restored.db"

    env = os.environ.copy()
    env["PULSECHECK_BACKUP_PASSPHRASE"] = PASSPHRASE

    result = subprocess.run(
        [
            sys.executable,
            "scripts/backup_crypto.py",
            "--encrypt-stdin",
            "--destination",
            str(encrypted),
        ],
        input=source_content,
        capture_output=True,
        env=env,
        check=True,
    )

    metadata = json.loads(result.stdout)
    assert metadata["size_bytes"] == encrypted.stat().st_size

    decrypt_file(encrypted, restored, PASSPHRASE)
    assert restored.read_bytes() == source_content
    assert temporary_files(tmp_path) == []
