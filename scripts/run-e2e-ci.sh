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

export PULSECHECK_TEST_SUBNET="172.30.${SELECTED_OCTET}.0/24"
export PULSECHECK_TEST_APP_IP="172.30.${SELECTED_OCTET}.2"
export PULSECHECK_TEST_SIMULATOR_IP="172.30.${SELECTED_OCTET}.10"

COMPOSE=(
    docker compose
    -p "$PROJECT"
    -f compose.yaml
    -f compose.test.yaml
)

cleanup() {
    echo "===== Cleaning up CI test environment ====="
    "${COMPOSE[@]}" down --volumes --remove-orphans || true
}

trap cleanup EXIT

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

if ! "${COMPOSE[@]}" exec -T pulsecheck \
    python /app/integration_test.py; then

    echo "===== INTEGRATION TEST FAILURE LOGS ====="
    "${COMPOSE[@]}" ps || true
    "${COMPOSE[@]}" logs --no-color \
        pulsecheck prometheus health-simulator || true
    exit 1
fi

echo "SUCCESS: Jenkins end-to-end integration stage passed."
