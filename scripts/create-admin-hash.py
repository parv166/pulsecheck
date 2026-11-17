#!/usr/bin/env python3

import os
import re
import sys
from getpass import getpass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.auth import hash_password


def main():
    username = input("Administrator username: ").strip()

    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", username):
        raise SystemExit("Invalid username.")

    password = getpass("Administrator password: ")
    confirmation = getpass("Confirm administrator password: ")

    if password != confirmation:
        raise SystemExit("Passwords do not match.")

    encoded = hash_password(password)

    secrets_dir = PROJECT_ROOT / "secrets"
    secrets_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(secrets_dir, 0o700)

    destination = secrets_dir / "auth.local.env"

    content = (
        f"PULSECHECK_ADMIN_USERNAME={username}\n"
        f"PULSECHECK_ADMIN_PASSWORD_HASH={encoded}\n"
        "PULSECHECK_COOKIE_SECURE=false\n"
    )

    fd = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )

    with os.fdopen(fd, "w") as file:
        file.write(content)

    os.chmod(destination, 0o600)

    gitignore = PROJECT_ROOT / ".gitignore"
    entry = "/secrets/auth.local.env"

    existing = gitignore.read_text() if gitignore.exists() else ""

    if entry not in existing.splitlines():
        with gitignore.open("a") as file:
            file.write("\n" + entry + "\n")

    print("PASS: Admin credentials stored locally.")
    print("File: secrets/auth.local.env")
    print("Permissions: 600")
    print("Git ignore: configured")
    print("Credential values were not displayed.")


if __name__ == "__main__":
    main()
