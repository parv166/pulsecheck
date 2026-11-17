import hashlib
import hmac
import secrets


SESSION_COOKIE_NAME = "pulsecheck_session"
SESSION_TTL_SECONDS = 8 * 60 * 60

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 64
SCRYPT_MAXMEM = 64 * 1024 * 1024


def hash_password(password: str) -> str:
    """Create a versioned scrypt password hash."""
    if not isinstance(password, str) or len(password) < 14:
        raise ValueError("Password must contain at least 14 characters.")

    salt = secrets.token_bytes(16)

    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=SCRYPT_MAXMEM,
    )

    return f"scrypt-v1${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded_hash: str) -> bool:
    """Verify a password against its stored scrypt hash."""
    try:
        scheme, salt_hex, digest_hex = encoded_hash.split("$")

        if scheme != "scrypt-v1":
            return False

        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)

        if len(salt) != 16 or len(expected) != SCRYPT_DKLEN:
            return False

        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            dklen=SCRYPT_DKLEN,
            maxmem=SCRYPT_MAXMEM,
        )

        return hmac.compare_digest(actual, expected)

    except (ValueError, TypeError, AttributeError):
        return False


def generate_session_token() -> str:
    """Generate a cryptographically random browser session token."""
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    """Hash a session token before storing it in SQLite."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_csrf_token() -> str:
    """Generate a CSRF token for authenticated browser requests."""
    return secrets.token_urlsafe(32)
