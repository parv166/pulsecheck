import pytest

from app.auth import (
    generate_csrf_token,
    generate_session_token,
    hash_password,
    hash_session_token,
    verify_password,
)
from app.database import Database


def test_password_hash_verifies_correct_password():
    password = "Temporary-Test-Password-2026!"
    encoded = hash_password(password)

    assert encoded.startswith("scrypt-v1$")
    assert verify_password(password, encoded)
    assert not verify_password("Wrong-password-2026!", encoded)


def test_password_hash_uses_unique_salts():
    password = "Temporary-Test-Password-2026!"

    assert hash_password(password) != hash_password(password)


def test_short_password_is_rejected():
    with pytest.raises(ValueError):
        hash_password("short")


def test_session_and_csrf_tokens_are_random():
    first = generate_session_token()
    second = generate_session_token()

    assert first != second
    assert generate_csrf_token() != generate_csrf_token()


def test_session_token_is_stored_as_hash():
    token = generate_session_token()
    token_hash = hash_session_token(token)

    assert token_hash != token
    assert len(token_hash) == 64


def test_database_session_lifecycle(tmp_path):
    database = Database(tmp_path / "auth-test.db")
    token_hash = hash_session_token(generate_session_token())

    database.create_session(
        token_hash=token_hash,
        username="test-admin",
        csrf_token=generate_csrf_token(),
        created_at="2026-11-17T00:00:00+00:00",
        expires_at="2026-11-18T00:00:00+00:00",
    )

    session = database.get_session(token_hash)

    assert session is not None
    assert session["username"] == "test-admin"

    assert database.delete_session(token_hash)
    assert database.get_session(token_hash) is None


def test_expired_sessions_are_removed(tmp_path):
    database = Database(tmp_path / "auth-expiry-test.db")
    token_hash = hash_session_token(generate_session_token())

    database.create_session(
        token_hash=token_hash,
        username="test-admin",
        csrf_token=generate_csrf_token(),
        created_at="2026-11-16T00:00:00+00:00",
        expires_at="2026-11-17T00:00:00+00:00",
    )

    removed = database.delete_expired_sessions(
        "2026-11-17T00:00:01+00:00"
    )

    assert removed == 1
    assert database.get_session(token_hash) is None
