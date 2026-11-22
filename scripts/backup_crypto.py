#!/usr/bin/env python3
"""Authenticated streaming encryption for PulseCheck database backups."""

import argparse
import hashlib
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


MAGIC = b"PCDBAK\x01"
SALT_LENGTH = 16
NONCE_LENGTH = 12
TAG_LENGTH = 16
CHUNK_SIZE = 1024 * 1024

SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1


def _validate_passphrase(passphrase: str) -> bytes:
    if not isinstance(passphrase, str):
        raise ValueError("Passphrase must be a string.")

    encoded = passphrase.encode("utf-8")
    if len(encoded) < 32:
        raise ValueError("Passphrase must contain at least 32 UTF-8 bytes.")

    return encoded


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(
        salt=salt,
        length=32,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
    ).derive(_validate_passphrase(passphrase))


def _validate_input(path: Path) -> Path:
    path = Path(path)

    if path.is_symlink() or not path.is_file():
        raise ValueError("Input must be an existing regular file, not a symlink.")

    return path


def _prepare_destination(source: Path, destination: Path) -> Path:
    destination = Path(destination)

    if not destination.parent.is_dir():
        raise NotADirectoryError("Destination directory does not exist.")

    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Refusing to overwrite an existing destination.")

    if source.resolve() == destination.resolve():
        raise ValueError("Source and destination must be different.")

    return destination


def _create_temp_file(destination: Path):
    fd, name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    return fd, Path(name)


def _publish(temp_path: Path, destination: Path) -> None:
    os.link(temp_path, destination)


def _file_metadata(path: Path) -> dict:
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)

    return {
        "sha256": digest.hexdigest(),
        "size_bytes": path.stat().st_size,
    }


def encrypt_stream(input_stream, destination_path: Path, passphrase: str) -> dict:
    """Encrypt a binary input stream without creating a plaintext file."""

    destination_path = Path(destination_path)

    if not destination_path.parent.is_dir():
        raise NotADirectoryError("Destination directory does not exist.")

    if destination_path.exists() or destination_path.is_symlink():
        raise FileExistsError("Refusing to overwrite an existing destination.")

    salt = secrets.token_bytes(SALT_LENGTH)
    nonce = secrets.token_bytes(NONCE_LENGTH)
    header = MAGIC + salt + nonce
    key = _derive_key(passphrase, salt)

    fd, temporary_path = _create_temp_file(destination_path)

    try:
        encryptor = Cipher(
            algorithms.AES(key),
            modes.GCM(nonce),
        ).encryptor()

        encryptor.authenticate_additional_data(header)

        with os.fdopen(fd, "wb") as output:
            fd = -1
            output.write(header)

            for chunk in iter(
                lambda: input_stream.read(CHUNK_SIZE),
                b"",
            ):
                output.write(encryptor.update(chunk))

            output.write(encryptor.finalize())
            output.write(encryptor.tag)
            output.flush()
            os.fsync(output.fileno())

        metadata = _file_metadata(temporary_path)
        _publish(temporary_path, destination_path)
        return metadata

    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass

        temporary_path.unlink(missing_ok=True)


def encrypt_file(
    source_path: Path,
    destination_path: Path,
    passphrase: str,
) -> dict:
    source_path = _validate_input(source_path)
    destination_path = _prepare_destination(
        source_path,
        Path(destination_path),
    )

    with source_path.open("rb") as source:
        return encrypt_stream(source, destination_path, passphrase)


def decrypt_file(
    source_path: Path,
    destination_path: Path,
    passphrase: str,
) -> dict:
    source_path = _validate_input(source_path)
    destination_path = _prepare_destination(
        source_path,
        Path(destination_path),
    )
    _validate_passphrase(passphrase)

    fd, temporary_path = _create_temp_file(destination_path)

    try:
        with source_path.open("rb") as source:
            file_size = os.fstat(source.fileno()).st_size
            header_length = len(MAGIC) + SALT_LENGTH + NONCE_LENGTH

            if file_size < header_length + TAG_LENGTH:
                raise ValueError("Encrypted backup is truncated.")

            header = source.read(header_length)

            if len(header) != header_length or not header.startswith(MAGIC):
                raise ValueError("Unsupported or invalid backup format.")

            salt_start = len(MAGIC)
            nonce_start = salt_start + SALT_LENGTH

            salt = header[salt_start:nonce_start]
            nonce = header[nonce_start:nonce_start + NONCE_LENGTH]

            source.seek(file_size - TAG_LENGTH)
            tag = source.read(TAG_LENGTH)

            if len(tag) != TAG_LENGTH:
                raise ValueError("Encrypted backup authentication tag is missing.")

            ciphertext_length = file_size - header_length - TAG_LENGTH
            source.seek(header_length)

            key = _derive_key(passphrase, salt)

            decryptor = Cipher(
                algorithms.AES(key),
                modes.GCM(nonce, tag),
            ).decryptor()

            decryptor.authenticate_additional_data(header)

            with os.fdopen(fd, "wb") as output:
                fd = -1
                remaining = ciphertext_length

                while remaining:
                    chunk = source.read(min(CHUNK_SIZE, remaining))

                    if not chunk:
                        raise ValueError("Encrypted backup is truncated.")

                    remaining -= len(chunk)
                    output.write(decryptor.update(chunk))

                try:
                    output.write(decryptor.finalize())
                except InvalidTag:
                    raise ValueError(
                        "Backup authentication failed: wrong passphrase "
                        "or modified encrypted file."
                    ) from None

                output.flush()
                os.fsync(output.fileno())

        metadata = _file_metadata(temporary_path)
        _publish(temporary_path, destination_path)
        return metadata

    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass

        temporary_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--encrypt", action="store_true")
    operation.add_argument("--encrypt-stdin", action="store_true")
    operation.add_argument("--decrypt", action="store_true")

    parser.add_argument("--source", type=Path)
    parser.add_argument("--destination", type=Path, required=True)

    args = parser.parse_args()

    passphrase = os.environ.get("PULSECHECK_BACKUP_PASSPHRASE")
    if not passphrase:
        parser.error("PULSECHECK_BACKUP_PASSPHRASE environment variable is required.")

    if args.encrypt_stdin:
        if args.source is not None:
            parser.error("--encrypt-stdin cannot be combined with --source")

        result = encrypt_stream(
            sys.stdin.buffer,
            args.destination,
            passphrase,
        )

    elif args.encrypt:
        if args.source is None:
            parser.error("--encrypt requires --source")

        result = encrypt_file(
            args.source,
            args.destination,
            passphrase,
        )

    else:
        if args.source is None:
            parser.error("--decrypt requires --source")

        result = decrypt_file(
            args.source,
            args.destination,
            passphrase,
        )

    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
