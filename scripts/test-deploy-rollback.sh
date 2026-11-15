#!/usr/bin/env bash
set -Eeuo pipefail

JENKINSFILE="${1:-Jenkinsfile}"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

# Extract the actual rollback function from the Jenkinsfile's remote script.
python3 - "$JENKINSFILE" "$WORK_DIR/rollback-function.sh" <<'PY'
from pathlib import Path
import sys

jenkinsfile = Path(sys.argv[1]).read_text()
marker = "<<'REMOTE_SCRIPT'\n"

if jenkinsfile.count(marker) != 1:
    raise SystemExit("Could not identify exactly one REMOTE_SCRIPT block.")

start = jenkinsfile.index(marker) + len(marker)
end = jenkinsfile.index("\nREMOTE_SCRIPT", start)
remote = jenkinsfile[start:end]

lines = remote.splitlines()
function_start = next(
    (i for i, line in enumerate(lines)
     if line.strip() == "rollback_and_cleanup() {"),
    None,
)

if function_start is None:
    raise SystemExit("rollback_and_cleanup() was not found.")

function_end = next(
    (i for i in range(function_start + 1, len(lines))
     if lines[i] == "}"),
    None,
)

if function_end is None:
    raise SystemExit("Could not find the end of rollback_and_cleanup().")

Path(sys.argv[2]).write_text(
    "\n".join(lines[function_start:function_end + 1]) + "\n"
)
print("Extracted actual rollback function from Jenkinsfile.")
PY

# Mock Docker. All state changes happen in temporary test files.
docker_mock() {
    printf 'docker %s\n' "$*" >> "$ACTION_LOG"

    case "$1" in
        inspect)
            grep -Fxq -- "$2" "$STATE_FILE"
            ;;

        rm)
            shift
            [[ "${1:-}" == "-f" ]] && shift
            local target="${1:?missing container name}"
            grep -Fvx -- "$target" "$STATE_FILE" > "$STATE_FILE.tmp" || true
            mv "$STATE_FILE.tmp" "$STATE_FILE"
            ;;

        rename)
            local old="$2"
            local new="$3"
            awk -v old="$old" -v new="$new" '
                $0 == old { print new; found=1; next }
                { print }
                END { if (!found) exit 1 }
            ' "$STATE_FILE" > "$STATE_FILE.tmp" &&
                mv "$STATE_FILE.tmp" "$STATE_FILE"
            ;;

        start|stop|logs)
            return 0
            ;;

        volume)
            return 0
            ;;

        *)
            echo "Unexpected mocked Docker command: $*" >&2
            return 99
            ;;
    esac
}

sudo() {
    if [[ "${1:-}" != "docker" ]]; then
        echo "Unexpected sudo command in test: $*" >&2
        return 99
    fi
    shift
    docker_mock "$@"
}

run_case() {
    local name="$1"
    local old_exists="$2"
    local switch_started="$3"
    local deployment_complete="$4"
    local initial_containers="$5"
    local expected_status="$6"

    STATE_FILE="$WORK_DIR/$name.containers"
    ACTION_LOG="$WORK_DIR/$name.actions"

    printf '%s\n' "$initial_containers" > "$STATE_FILE"
    : > "$ACTION_LOG"

    local actual_status
    if (
        set +e

        export STATE_FILE ACTION_LOG
        APP_CONTAINER="pulsecheck-app"
        PREVIOUS_CONTAINER="pulsecheck-app-previous"
        CANDIDATE_CONTAINER="pulsecheck-candidate-test"
        CANDIDATE_VOLUME="pulsecheck-candidate-test-data"

        OLD_CONTAINER_EXISTS="$old_exists"
        PRODUCTION_SWITCH_STARTED="$switch_started"
        DEPLOYMENT_COMPLETE="$deployment_complete"
        CURL_OK=1

        cleanup_ecr_credentials() { :; }
        sleep() { :; }
        curl() {
            printf 'curl %s\n' "$*" >> "$ACTION_LOG"
            [[ "$CURL_OK" == "1" ]]
        }

        source "$WORK_DIR/rollback-function.sh"

        # Reproduce the real Jenkins EXIT trap behavior.
        trap rollback_and_cleanup EXIT

        if [[ "$deployment_complete" == "1" ]]; then
            exit 0
        else
            exit 1
        fi
    ); then
        actual_status=0
    else
        actual_status=$?
    fi

    if [[ "$actual_status" -ne "$expected_status" ]]; then
        echo "FAIL: $name - expected exit $expected_status, got $actual_status"
        cat "$ACTION_LOG"
        return 1
    fi
}

# 1. Candidate check fails before production switch.
# Current production and older rollback container must both remain untouched.
run_case \
    "candidate-failure" 1 0 0 \
    $'pulsecheck-app\npulsecheck-app-previous\npulsecheck-candidate-test' \
    1

grep -Fxq "pulsecheck-app" "$WORK_DIR/candidate-failure.containers"
grep -Fxq "pulsecheck-app-previous" "$WORK_DIR/candidate-failure.containers"

if grep -Fq "docker rm -f pulsecheck-app" "$WORK_DIR/candidate-failure.actions"; then
    echo "FAIL: candidate failure removed current production."
    exit 1
fi

echo "PASS: candidate failure preserves current production and previous container."

# 2. Production switch has started and the new application fails.
# Previous container must be restored as the application.
run_case \
    "switch-failure" 1 1 0 \
    $'pulsecheck-app\npulsecheck-app-previous' \
    1

grep -Fxq "pulsecheck-app" "$WORK_DIR/switch-failure.containers"

if grep -Fxq "pulsecheck-app-previous" "$WORK_DIR/switch-failure.containers"; then
    echo "FAIL: previous container was not renamed back."
    exit 1
fi

grep -Fq "docker rename pulsecheck-app-previous pulsecheck-app" \
    "$WORK_DIR/switch-failure.actions"
grep -Fq "docker start pulsecheck-app" "$WORK_DIR/switch-failure.actions"

echo "PASS: production-switch failure restores the previous container."

# 3. Deployment completed successfully.
# The new app and rollback container must remain in place.
run_case \
    "successful-deployment" 1 1 1 \
    $'pulsecheck-app\npulsecheck-app-previous' \
    0

grep -Fxq "pulsecheck-app" "$WORK_DIR/successful-deployment.containers"
grep -Fxq "pulsecheck-app-previous" "$WORK_DIR/successful-deployment.containers"

if grep -Fq "docker rm -f pulsecheck-app" "$WORK_DIR/successful-deployment.actions"; then
    echo "FAIL: successful deployment unexpectedly removed production."
    exit 1
fi

echo "PASS: successful deployment leaves production and rollback containers intact."
echo
echo "All three mocked rollback scenarios passed."
