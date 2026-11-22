#!/usr/bin/env bash
set -Eeuo pipefail

: "${BUILD_NUMBER:?BUILD_NUMBER is required}"

if [[ ! "$BUILD_NUMBER" =~ ^[0-9]+$ ]]; then
    echo "ERROR: BUILD_NUMBER must be numeric."
    exit 1
fi

BUILD_ID=$((10#$BUILD_NUMBER))
PROJECT="pulsecheck-ci-${BUILD_NUMBER}"
IMAGE="${PULSECHECK_IMAGE:-pulsecheck:${BUILD_NUMBER}}"

cd "$(dirname "$0")/.."

# Select an unused Docker subnet.
NETWORK_IDS=$(docker network ls -q)
USED_SUBNETS=""

if [[ -n "$NETWORK_IDS" ]]; then
    USED_SUBNETS=$(
        docker network inspect $NETWORK_IDS \
          --format '{{range .IPAM.Config}}{{println .Subnet}}{{end}}' \
          2>/dev/null || true
    )
fi

SELECTED_OCTET=""

for offset in $(seq 0 199); do
    octet=$((20 + ((BUILD_ID % 200 + offset) % 200)))
    candidate="172.30.${octet}.0/24"

    if ! grep -Fxq "$candidate" <<< "$USED_SUBNETS"; then
        SELECTED_OCTET="$octet"
        break
    fi
done

if [[ -z "$SELECTED_OCTET" ]]; then
    echo "ERROR: No unused test subnet found."
    exit 1
fi

export PULSECHECK_IMAGE="$IMAGE"
export PULSECHECK_HOST_PORT=$((20000 + BUILD_ID % 10000))
export PROMETHEUS_HOST_PORT=$((30000 + BUILD_ID % 10000))
export GRAFANA_ADMIN_PASSWORD="ci-test-placeholder"

# Generate a unique, temporary admin credential for this CI run.
AUTH_CREDENTIALS=$(
    docker run --rm \
        --entrypoint /app/venv/bin/python \
        "$IMAGE" \
        -c 'import secrets; from app.auth import hash_password; password = secrets.token_urlsafe(32); print(password); print(hash_password(password))'
)

mapfile -t AUTH_LINES <<< "$AUTH_CREDENTIALS"

if [[ "${#AUTH_LINES[@]}" -ne 2 || -z "${AUTH_LINES[0]}" || -z "${AUTH_LINES[1]}" ]]; then
    echo "ERROR: Could not generate temporary CI authentication credentials."
    exit 1
fi

export PULSECHECK_ADMIN_USERNAME="ci-admin"
export PULSECHECK_ADMIN_PASSWORD_HASH="${AUTH_LINES[1]}"
export PULSECHECK_E2E_ADMIN_PASSWORD="${AUTH_LINES[0]}"

unset AUTH_CREDENTIALS AUTH_LINES

export PULSECHECK_TEST_SUBNET="172.30.${SELECTED_OCTET}.0/24"
export PULSECHECK_TEST_APP_IP="172.30.${SELECTED_OCTET}.2"
export PULSECHECK_TEST_SIMULATOR_IP="172.30.${SELECTED_OCTET}.10"

COMPOSE=(
    docker compose
    -p "$PROJECT"
    -f compose.yaml
    -f compose.test.yaml
)

AUTH_ENV_FILE="secrets/auth.local.env"
AUTH_ENV_FILE_CREATED=0
SECRETS_DIR_CREATED=0

cleanup() {
    echo "===== Cleaning up CI test environment ====="
    "${COMPOSE[@]}" down --volumes --remove-orphans || true

    if [[ "$AUTH_ENV_FILE_CREATED" -eq 1 ]]; then
        rm -f -- "$AUTH_ENV_FILE" || true
    fi

    if [[ "$SECRETS_DIR_CREATED" -eq 1 ]]; then
        rmdir -- secrets 2>/dev/null || true
    fi
}

trap cleanup EXIT

# The base Compose file requires this local env_file. CI credentials are
# supplied by compose.test.yaml, so create an empty placeholder when absent.
if [[ -L "$AUTH_ENV_FILE" ]]; then
    echo "ERROR: Refusing to use a symlink for the CI auth env file."
    exit 1
elif [[ ! -e "$AUTH_ENV_FILE" ]]; then
    if [[ ! -d secrets ]]; then
        if [[ -e secrets || -L secrets ]]; then
            echo "ERROR: secrets exists but is not a directory."
            exit 1
        fi
        mkdir -m 700 secrets
        SECRETS_DIR_CREATED=1
    fi

    (umask 077 && : > "$AUTH_ENV_FILE")
    AUTH_ENV_FILE_CREATED=1
elif [[ ! -f "$AUTH_ENV_FILE" ]]; then
    echo "ERROR: CI auth env path exists but is not a regular file."
    exit 1
fi

echo "===== E2E CONFIGURATION ====="
echo "Image: $PULSECHECK_IMAGE"
echo "Project: $PROJECT"
echo "Test subnet: $PULSECHECK_TEST_SUBNET"

"${COMPOSE[@]}" config -q

echo "===== STARTING TEST ENVIRONMENT ====="

if ! "${COMPOSE[@]}" up -d --no-build \
    pulsecheck prometheus health-simulator; then

    "${COMPOSE[@]}" logs --no-color \
        pulsecheck prometheus health-simulator || true
    exit 1
fi

echo "===== RUNNING UP -> DOWN -> UP TEST ====="

if ! "${COMPOSE[@]}" exec -T \
    -e "PULSECHECK_E2E_ADMIN_PASSWORD=${PULSECHECK_E2E_ADMIN_PASSWORD}" \
    pulsecheck python /app/integration_test.py; then

    echo "===== INTEGRATION TEST FAILURE LOGS ====="
    "${COMPOSE[@]}" ps || true
    "${COMPOSE[@]}" logs --no-color \
        pulsecheck prometheus health-simulator || true
    exit 1
fi

echo "SUCCESS: Jenkins end-to-end integration stage passed."
