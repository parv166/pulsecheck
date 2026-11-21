#!/usr/bin/env bash
set -Eeuo pipefail

IMAGE="${PULSECHECK_IMAGE:-${1:-}}"
BUILD_ID="${BUILD_NUMBER:-manual-$(date +%s)}"

if [[ -z "$IMAGE" ]]; then
    echo "ERROR: Set PULSECHECK_IMAGE."
    exit 1
fi

docker image inspect "$IMAGE" >/dev/null

if ! docker info >/dev/null 2>&1; then
    echo "ERROR: Docker is unavailable."
    exit 1
fi

PREFIX="pulsecheck-migration-test-${BUILD_ID}"
SOURCE_VOLUME="${PREFIX}-source"
DEST_VOLUME="${PREFIX}-destination"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="$SCRIPT_DIR/backup_sqlite_volume.py"

if [[ ! -f "$HELPER" ]]; then
    echo "ERROR: Shared SQLite backup helper is missing."
    exit 1
fi

for volume in "$SOURCE_VOLUME" "$DEST_VOLUME"; do
    if docker volume inspect "$volume" >/dev/null 2>&1; then
        echo "ERROR: Refusing to overwrite existing volume: $volume"
        exit 1
    fi
done

WORK_DIR="$(mktemp -d)"

cleanup() {
    local status=$?
    trap - EXIT
    set +e

    echo "===== CLEANING MIGRATION TEST VOLUMES ====="
    docker volume rm "$DEST_VOLUME" "$SOURCE_VOLUME" >/dev/null 2>&1 || true
    rm -rf "$WORK_DIR"

    if [[ "$status" -eq 0 ]]; then
        echo "PASS: Migration test resources cleaned."
    else
        echo "Migration test failed; cleanup attempted."
    fi

    exit "$status"
}

trap cleanup EXIT

docker volume create "$SOURCE_VOLUME" >/dev/null
docker volume create "$DEST_VOLUME" >/dev/null

echo "===== PREPARING SOURCE VOLUME ====="

docker run --rm --user 0:0 \
    --mount "type=volume,source=$SOURCE_VOLUME,target=/app/data" \
    --entrypoint /app/venv/bin/python \
    "$IMAGE" \
    -c 'import os; os.chown("/app/data",65532,65532); os.chmod("/app/data",0o750)'

echo "===== CREATING REAL PULSECHECK DATABASE RECORDS ====="

SEED_PROGRAM=$(cat <<'PY'
from datetime import datetime, timedelta, timezone
import sqlite3

from app.database import Database
from app.monitoring import CheckResult, MonitoredTarget

path = "/app/data/pulsecheck.db"
database = Database(path)

target_a = MonitoredTarget(
    id="migration-resolved-target",
    name="Migration Resolved Target",
    url="https://example.com/healthy",
)

target_b = MonitoredTarget(
    id="migration-active-target",
    name="Migration Active Target",
    url="https://example.org/health",
)

database.save_target(target_a)
database.save_target(target_b)

now = datetime.now(timezone.utc)
previous = now - timedelta(minutes=2)
recovered = now - timedelta(minutes=1)

database.save_results([
    CheckResult(
        target_id=target_a.id,
        name=target_a.name,
        url=target_a.url,
        status="DOWN",
        status_code=503,
        latency_ms=120.5,
        checked_at=previous,
        consecutive_failures=2,
        last_success_at=previous - timedelta(minutes=1),
        error="HTTP 503",
    ),
    CheckResult(
        target_id=target_a.id,
        name=target_a.name,
        url=target_a.url,
        status="UP",
        status_code=200,
        latency_ms=42.5,
        checked_at=recovered,
        consecutive_failures=0,
        last_success_at=recovered,
        error=None,
    ),
    CheckResult(
        target_id=target_b.id,
        name=target_b.name,
        url=target_b.url,
        status="DOWN",
        status_code=None,
        latency_ms=1500.0,
        checked_at=now,
        consecutive_failures=3,
        last_success_at=None,
        error="Connection timeout",
    ),
])

with sqlite3.connect(path) as db:
    db.execute("""
        INSERT INTO auth_sessions(
            token_hash, username, csrf_token, created_at, expires_at
        )
        VALUES (?, ?, ?, ?, ?)
    """, (
        "migration-session-hash",
        "migration-test-admin",
        "migration-csrf-token",
        now.isoformat(),
        (now + timedelta(hours=1)).isoformat(),
    ))

    for table in (
        "monitored_targets",
        "monitoring_history",
        "incidents",
        "auth_sessions",
    ):
        count = db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        print(f"Source {table}: {count}")

    assert db.execute(
        "SELECT COUNT(*) FROM monitored_targets"
    ).fetchone()[0] == 2

    assert db.execute(
        "SELECT COUNT(*) FROM monitoring_history"
    ).fetchone()[0] == 3

    assert db.execute(
        "SELECT COUNT(*) FROM incidents WHERE status='RESOLVED'"
    ).fetchone()[0] == 1

    assert db.execute(
        "SELECT COUNT(*) FROM incidents WHERE status='ACTIVE'"
    ).fetchone()[0] == 1

    assert db.execute(
        "SELECT COUNT(*) FROM auth_sessions"
    ).fetchone()[0] == 1

print("PASS: Real PulseCheck source records created.")
PY
)

docker run --rm --user 65532:65532 \
    --mount "type=volume,source=$SOURCE_VOLUME,target=/app/data" \
    --entrypoint /app/venv/bin/python \
    "$IMAGE" -c "$SEED_PROGRAM"

echo "===== RUNNING THE SHARED PRODUCTION BACKUP HELPER ====="

docker run --rm --user 0:0 \
    --mount "type=volume,source=$SOURCE_VOLUME,target=/source" \
    --mount "type=volume,source=$DEST_VOLUME,target=/dest" \
    --mount "type=bind,source=$HELPER,target=/backup_sqlite_volume.py,readonly" \
    --entrypoint /app/venv/bin/python \
    "$IMAGE" \
    /backup_sqlite_volume.py \
    --source /source \
    --destination /dest

echo "===== VERIFYING COMPLETE SOURCE/DESTINATION DATA EQUALITY ====="

VERIFY_PROGRAM=$(cat <<'PY'
import os
import sqlite3
import stat

source_path = "/source/pulsecheck.db"
destination_path = "/dest/pulsecheck.db"

tables = (
    "monitored_targets",
    "monitoring_history",
    "incidents",
    "auth_sessions",
)

def snapshot(path):
    with sqlite3.connect(path) as db:
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        assert integrity == "ok", f"Integrity failed: {path}"

        return {
            table: db.execute(
                f'SELECT * FROM "{table}" ORDER BY rowid'
            ).fetchall()
            for table in tables
        }

source = snapshot(source_path)
destination = snapshot(destination_path)

for table in tables:
    assert source[table] == destination[table], (
        f"Source and destination differ in {table}"
    )
    print(f"PASS: {table}: {len(source[table])} identical records")

with sqlite3.connect(destination_path) as db:
    assert db.execute(
        "SELECT COUNT(*) FROM incidents WHERE status='ACTIVE'"
    ).fetchone()[0] == 1

    assert db.execute(
        "SELECT COUNT(*) FROM incidents WHERE status='RESOLVED'"
    ).fetchone()[0] == 1

database_stat = os.stat(destination_path)
directory_stat = os.stat("/dest")

assert database_stat.st_uid == 65532
assert database_stat.st_gid == 65532
assert stat.S_IMODE(database_stat.st_mode) == 0o640

assert directory_stat.st_uid == 65532
assert directory_stat.st_gid == 65532
assert stat.S_IMODE(directory_stat.st_mode) == 0o750

print("PASS: Destination database ownership 65532:65532")
print("PASS: Destination database permissions 0640")
print("PASS: Destination directory ownership 65532:65532")
print("PASS: Destination directory permissions 0750")
print("PASS: Complete SQLite migration verification.")
PY
)

docker run --rm --user 0:0 \
    --mount "type=volume,source=$SOURCE_VOLUME,target=/source" \
    --mount "type=volume,source=$DEST_VOLUME,target=/dest" \
    --entrypoint /app/venv/bin/python \
    "$IMAGE" -c "$VERIFY_PROGRAM"

echo
echo "SQLITE VOLUME MIGRATION REGRESSION TEST PASSED."
