#!/usr/bin/env bash
set -Eeuo pipefail

IMAGE="${PULSECHECK_IMAGE:-${1:-}}"
BUILD_ID="${BUILD_NUMBER:-manual-$(date +%s)}"

if [[ -z "$IMAGE" ]]; then
    echo "ERROR: Set PULSECHECK_IMAGE to a locally available image."
    exit 1
fi

if [[ ! "$BUILD_ID" =~ ^[A-Za-z0-9_.-]{1,40}$ ]]; then
    echo "ERROR: Invalid BUILD_NUMBER."
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo "ERROR: Docker is unavailable."
    exit 1
fi

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "ERROR: Image is not available locally: $IMAGE"
    exit 1
fi

# The real Jenkins rollback function checks port 8000.
python3 - <<'PY'
import socket

sock = socket.socket()
try:
    sock.bind(("127.0.0.1", 8000))
except OSError as exc:
    raise SystemExit(f"ERROR: Test port 8000 is unavailable: {exc}")
finally:
    sock.close()
PY

PREFIX="pulsecheck-real-rollback-${BUILD_ID}"

APP_CONTAINER="${PREFIX}-app"
PREVIOUS_CONTAINER="${PREFIX}-previous"
CANDIDATE_CONTAINER="${PREFIX}-candidate"
OLD_VOLUME="${PREFIX}-old-data"
CANDIDATE_VOLUME="${PREFIX}-candidate-data"

EXPECTED_VERSION="rollback-test-${BUILD_ID}"
EXPECTED_MARKER="real-docker-rollback-${BUILD_ID}"
EXPECTED_VALUE="preserved-by-real-docker-rollback"

echo "===== CHECKING FOR RESOURCE NAME COLLISIONS ====="

for name in "$APP_CONTAINER" "$PREVIOUS_CONTAINER" "$CANDIDATE_CONTAINER"; do
    if docker container inspect "$name" >/dev/null 2>&1; then
        echo "ERROR: Container already exists: $name"
        exit 1
    fi
done

for name in "$OLD_VOLUME" "$CANDIDATE_VOLUME"; do
    if docker volume inspect "$name" >/dev/null 2>&1; then
        echo "ERROR: Volume already exists: $name"
        exit 1
    fi
done

WORK_DIR="$(mktemp -d)"
CLEANUP_CONTAINERS=(
    "$APP_CONTAINER"
    "$PREVIOUS_CONTAINER"
    "$CANDIDATE_CONTAINER"
)
CLEANUP_VOLUMES=(
    "$CANDIDATE_VOLUME"
    "$OLD_VOLUME"
)

cleanup() {
    local exit_status=$?
    trap - EXIT
    set +e

    echo "===== CLEANING ISOLATED ROLLBACK TEST RESOURCES ====="

    for name in "${CLEANUP_CONTAINERS[@]}"; do
        docker rm -f "$name" >/dev/null 2>&1 || true
    done

    for name in "${CLEANUP_VOLUMES[@]}"; do
        docker volume rm "$name" >/dev/null 2>&1 || true
    done

    rm -rf "$WORK_DIR"

    if [[ "$exit_status" -eq 0 ]]; then
        echo "PASS: Isolated test resources cleaned."
    else
        echo "Test failed. Cleanup attempted."
    fi

    exit "$exit_status"
}

trap cleanup EXIT

echo "===== EXTRACTING ACTUAL JENKINS ROLLBACK FUNCTIONS ====="

python3 - "$PWD/Jenkinsfile" "$WORK_DIR/functions.sh" <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_text()
marker = "<<'REMOTE_SCRIPT'\n"

if source.count(marker) != 1:
    raise SystemExit("Expected exactly one REMOTE_SCRIPT block.")

start = source.index(marker) + len(marker)
end = source.index("\nREMOTE_SCRIPT", start)
lines = source[start:end].splitlines()

blocks = []

for name in ("wait_for_app", "rollback_and_cleanup"):
    first = next(
        (i for i, line in enumerate(lines)
         if line.strip() == f"{name}() {{"),
        None,
    )
    if first is None:
        raise SystemExit(f"Could not find {name}().")

    last = next(
        (i for i in range(first + 1, len(lines))
         if lines[i] == "}"),
        None,
    )
    if last is None:
        raise SystemExit(f"Could not find end of {name}().")

    blocks.append("\n".join(lines[first:last + 1]))

Path(sys.argv[2]).write_text("\n\n".join(blocks) + "\n")
print("PASS: Extracted actual Jenkins functions.")
PY

source "$WORK_DIR/functions.sh"

# Jenkins already has Docker access. This wrapper lets the extracted
# production function use that same Docker daemon without requiring
# passwordless sudo on the Jenkins agent.
sudo() {
    if [[ "${1:-}" == "docker" ]]; then
        shift
        command docker "$@"
    else
        command sudo "$@"
    fi
}

cleanup_ecr_credentials() {
    :
}

echo "===== CREATING ISOLATED DOCKER VOLUMES ====="

docker volume create "$OLD_VOLUME" >/dev/null
docker volume create "$CANDIDATE_VOLUME" >/dev/null

echo "===== PREPARING ORIGINAL V56-STYLE DATABASE VOLUME ====="

docker run --rm \
    --user 0:0 \
    --mount "type=volume,source=$OLD_VOLUME,target=/app/data" \
    --entrypoint /app/venv/bin/python \
    "$IMAGE" \
    -c 'import os; os.chown("/app/data",65532,65532); os.chmod("/app/data",0o750)'

echo "===== STARTING ORIGINAL APPLICATION ====="

docker run -d \
    --name "$APP_CONTAINER" \
    --read-only \
    --cap-drop=ALL \
    --security-opt=no-new-privileges:true \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \
    --pids-limit=100 \
    --mount "type=volume,source=$OLD_VOLUME,target=/app/data" \
    -p 127.0.0.1:8000:8000 \
    -e "APP_VERSION=$EXPECTED_VERSION" \
    -e PULSECHECK_DB_PATH=/app/data/pulsecheck.db \
    -e PULSECHECK_CHECK_INTERVAL_SECONDS=3600 \
    "$IMAGE" >/dev/null

echo "===== WAITING FOR ORIGINAL APPLICATION HEALTH ====="

ready=0
for attempt in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1 \
       && curl -fsS http://127.0.0.1:8000/version >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 2
done

if [[ "$ready" -ne 1 ]]; then
    docker logs "$APP_CONTAINER" || true
    echo "ERROR: Original application did not become healthy."
    exit 1
fi

echo "===== WRITING RECOVERY MARKER INTO SQLITE ====="

docker exec "$APP_CONTAINER" /app/venv/bin/python -c "
import sqlite3

with sqlite3.connect('/app/data/pulsecheck.db') as db:
    db.execute('''
        CREATE TABLE IF NOT EXISTS rollback_test_marker (
            marker TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    ''')
    db.execute(
        'INSERT INTO rollback_test_marker(marker,value) VALUES (?,?)',
        ('$EXPECTED_MARKER','$EXPECTED_VALUE')
    )

print('Recovery marker committed.')
"

echo "===== STARTING DISPOSABLE CANDIDATE CONTAINER ====="

docker run -d \
    --name "$CANDIDATE_CONTAINER" \
    --read-only \
    --cap-drop=ALL \
    --security-opt=no-new-privileges:true \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \
    --pids-limit=100 \
    --mount "type=volume,source=$CANDIDATE_VOLUME,target=/app/data" \
    --entrypoint /app/venv/bin/python \
    "$IMAGE" \
    -c 'import time; time.sleep(3600)' >/dev/null

echo "===== SIMULATING FAILED PRODUCTION SWITCH ====="

EXPECTED_FAILURE_MARKER="$WORK_DIR/forced-failure"

if (
    set -Eeuo pipefail

    OLD_CONTAINER_EXISTS=1
    OLD_CONTAINER_STOPPED=0
    PRODUCTION_SWITCH_STARTED=0
    DEPLOYMENT_COMPLETE=0
    DOCKER_CONFIG_DIR=""

    trap rollback_and_cleanup EXIT

    sudo docker stop -t 30 "$APP_CONTAINER" >/dev/null
    OLD_CONTAINER_STOPPED=1

    PRODUCTION_SWITCH_STARTED=1

    sudo docker rename "$APP_CONTAINER" "$PREVIOUS_CONTAINER"

    echo "Starting deliberately non-serving candidate application..."

    sudo docker run -d \
        --name "$APP_CONTAINER" \
        --read-only \
        --cap-drop=ALL \
        --security-opt=no-new-privileges:true \
        --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \
        --pids-limit=100 \
        --mount "type=volume,source=$CANDIDATE_VOLUME,target=/app/data" \
        -p 127.0.0.1:8000:8000 \
        --entrypoint /app/venv/bin/python \
        "$IMAGE" \
        -c 'import time; time.sleep(3600)' >/dev/null

    sleep 2

    echo "Forcing expected deployment failure."
    touch "$EXPECTED_FAILURE_MARKER"
    false
) 2>&1 | tee "$WORK_DIR/rollback-output.log"; then
    echo "ERROR: Simulated deployment unexpectedly succeeded."
    exit 1
else
    scenario_status=$?
fi

if [[ "$scenario_status" -ne 1 ]] \
   || [[ ! -f "$EXPECTED_FAILURE_MARKER" ]]; then
    echo "ERROR: Failure simulation did not reach its intended point."
    exit 1
fi

if ! grep -Fq "PASS: Previous production application restored." \
    "$WORK_DIR/rollback-output.log"; then
    echo "ERROR: Jenkins did not confirm successful restoration."
    exit 1
fi

echo "===== VERIFYING RESTORED APPLICATION ====="

RECOVERED_IMAGE=$(docker inspect "$APP_CONTAINER" --format '{{.Config.Image}}')
RECOVERED_STATUS=$(docker inspect "$APP_CONTAINER" --format '{{.State.Status}}')
RECOVERED_VOLUME=$(docker inspect "$APP_CONTAINER" --format \
    '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Name}}{{end}}{{end}}')

[[ "$RECOVERED_IMAGE" == "$IMAGE" ]] || {
    echo "ERROR: Restored image mismatch: $RECOVERED_IMAGE"
    exit 1
}

[[ "$RECOVERED_STATUS" == "running" ]] || {
    echo "ERROR: Restored application is not running."
    exit 1
}

[[ "$RECOVERED_VOLUME" == "$OLD_VOLUME" ]] || {
    echo "ERROR: Original database volume was not restored."
    exit 1
}

curl -fsS http://127.0.0.1:8000/health
echo

VERSION_JSON=$(curl -fsS http://127.0.0.1:8000/version)
echo "$VERSION_JSON"

python3 - "$VERSION_JSON" "$EXPECTED_VERSION" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
expected = sys.argv[2]

assert payload["version"] == expected, (
    f"Expected restored version {expected}, got {payload['version']}"
)
print("PASS: Original application version restored.")
PY

echo "===== VERIFYING SQLITE INTEGRITY AND DATA RECOVERY ====="

docker exec "$APP_CONTAINER" /app/venv/bin/python -c "
import sqlite3

with sqlite3.connect('/app/data/pulsecheck.db') as db:
    integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
    marker = db.execute(
        'SELECT marker,value FROM rollback_test_marker'
    ).fetchone()

print('SQLite integrity:', integrity)
print('Recovery marker:', marker)

assert integrity == 'ok'
assert marker == ('$EXPECTED_MARKER','$EXPECTED_VALUE')
"

echo "===== VERIFYING FAILED CANDIDATE CLEANUP ====="

for name in "$PREVIOUS_CONTAINER" "$CANDIDATE_CONTAINER"; do
    if docker container inspect "$name" >/dev/null 2>&1; then
        echo "ERROR: Failed candidate resource remains: $name"
        exit 1
    fi
done

if docker volume inspect "$CANDIDATE_VOLUME" >/dev/null 2>&1; then
    echo "ERROR: Failed candidate volume remains."
    exit 1
fi

docker volume inspect "$OLD_VOLUME" >/dev/null

echo
echo "REAL DOCKER DEPLOYMENT ROLLBACK TEST PASSED."
