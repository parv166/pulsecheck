#!/usr/bin/env bash
set -Eeuo pipefail

JENKINSFILE="${1:-Jenkinsfile}"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

# Extract the real health-check and rollback functions from Jenkinsfile.
python3 - "$JENKINSFILE" "$WORK_DIR/rollback-functions.sh" <<'PY'
from pathlib import Path
import sys

jenkinsfile = Path(sys.argv[1]).read_text()
marker = "<<'REMOTE_SCRIPT'\n"

if jenkinsfile.count(marker) != 1:
    raise SystemExit("Could not identify exactly one REMOTE_SCRIPT block.")

start = jenkinsfile.index(marker) + len(marker)
end = jenkinsfile.index("\nREMOTE_SCRIPT", start)
lines = jenkinsfile[start:end].splitlines()

blocks = []

for name in ("wait_for_app", "rollback_and_cleanup"):
    function_start = next(
        (i for i, line in enumerate(lines)
         if line.strip() == f"{name}() {{"),
        None,
    )

    if function_start is None:
        raise SystemExit(f"{name}() was not found.")

    function_end = next(
        (i for i in range(function_start + 1, len(lines))
         if lines[i] == "}"),
        None,
    )

    if function_end is None:
        raise SystemExit(f"Could not find the end of {name}().")

    blocks.append("\n".join(lines[function_start:function_end + 1]))

Path(sys.argv[2]).write_text("\n\n".join(blocks) + "\n")
print("Extracted actual wait_for_app() and rollback_and_cleanup().")
PY

# All Docker operations are mocked and operate only on temporary files.
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

            grep -Fvx -- "$target" "$STATE_FILE" \
                > "$STATE_FILE.tmp" || true
            mv "$STATE_FILE.tmp" "$STATE_FILE"
            ;;

        rename)
            if [[ "${MOCK_FAIL_RENAME:-0}" == "1" ]]; then
                return 1
            fi

            local old="$2"
            local new="$3"

            awk -v old="$old" -v new="$new" '
                $0 == old { print new; found=1; next }
                { print }
                END { if (!found) exit 1 }
            ' "$STATE_FILE" > "$STATE_FILE.tmp" &&
                mv "$STATE_FILE.tmp" "$STATE_FILE"
            ;;

        start)
            if [[ "${MOCK_FAIL_START:-0}" == "1" ]]; then
                return 1
            fi
            grep -Fxq -- "$2" "$STATE_FILE"
            ;;

        stop|logs)
            return 0
            ;;

        volume)
            if [[ "${2:-}" == "rm" ]]; then
                local target="${3:?missing volume name}"
                grep -Fvx -- "$target" "$VOLUME_STATE_FILE" \
                    > "$VOLUME_STATE_FILE.tmp" || true
                mv "$VOLUME_STATE_FILE.tmp" "$VOLUME_STATE_FILE"
            else
                echo "Unexpected mocked Docker volume operation: $*" >&2
                return 99
            fi
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
    local old_stopped="$3"
    local switch_started="$4"
    local deployment_complete="$5"
    local initial_containers="$6"
    local expected_status="$7"
    local curl_ok="$8"

    STATE_FILE="$WORK_DIR/$name.containers"
    VOLUME_STATE_FILE="$WORK_DIR/$name.volumes"
    ACTION_LOG="$WORK_DIR/$name.actions"
    CASE_OUTPUT="$WORK_DIR/$name.output"

    printf '%s\n' "$initial_containers" > "$STATE_FILE"
    printf '%s\n' "pulsecheck-data-old" "pulsecheck-candidate-test-data" > "$VOLUME_STATE_FILE"
    : > "$ACTION_LOG"
    : > "$CASE_OUTPUT"

    local actual_status

    set +e
    (
        set -Eeuo pipefail

        export STATE_FILE VOLUME_STATE_FILE ACTION_LOG
        APP_CONTAINER="pulsecheck-app"
        PREVIOUS_CONTAINER="pulsecheck-app-previous"
        CANDIDATE_CONTAINER="pulsecheck-candidate-test"
        CANDIDATE_VOLUME="pulsecheck-candidate-test-data"
        OLD_VOLUME="pulsecheck-data-old"

        OLD_CONTAINER_EXISTS="$old_exists"
        OLD_CONTAINER_STOPPED="$old_stopped"
        PRODUCTION_SWITCH_STARTED="$switch_started"
        DEPLOYMENT_COMPLETE="$deployment_complete"
        CANDIDATE_CONTAINER_CREATED=1
        CANDIDATE_VOLUME_CREATED=1
        CURL_OK="$curl_ok"

        cleanup_ecr_credentials() { :; }
        cleanup_remote_assets() { :; }
        sleep() { :; }

        curl() {
            printf 'curl %s\n' "$*" >> "$ACTION_LOG"
            [[ "$CURL_OK" == "1" ]]
        }

        source "$WORK_DIR/rollback-functions.sh"

        trap rollback_and_cleanup EXIT

        if [[ "$deployment_complete" == "1" ]]; then
            exit 0
        else
            exit 1
        fi
    ) > "$CASE_OUTPUT" 2>&1
    actual_status=$?
    set -e

    if [[ "$actual_status" -ne "$expected_status" ]]; then
        echo "FAIL: $name - expected exit $expected_status, got $actual_status"
        cat "$CASE_OUTPUT"
        cat "$ACTION_LOG"
        return 1
    fi
}

assert_container_present() {
    local name="$1"
    local container="$2"

    if ! grep -Fxq -- "$container" "$WORK_DIR/$name.containers"; then
        echo "FAIL: $name - expected container missing: $container"
        exit 1
    fi
}

assert_container_absent() {
    local name="$1"
    local container="$2"

    if grep -Fxq -- "$container" "$WORK_DIR/$name.containers"; then
        echo "FAIL: $name - unexpected container remains: $container"
        exit 1
    fi
}

assert_start_called() {
    local name="$1"

    if ! grep -Fxq "docker start pulsecheck-app" "$WORK_DIR/$name.actions"; then
        echo "FAIL: $name - original application was not explicitly started."
        exit 1
    fi
}

assert_health_checks() {
    local name="$1"
    local actions="$WORK_DIR/$name.actions"

    grep -Fq 'curl -fsS http://127.0.0.1:8000/health' "$actions" || {
        echo "FAIL: $name - health endpoint was not checked."
        exit 1
    }

    grep -Fq 'curl -fsS http://127.0.0.1:8000/version' "$actions" || {
        echo "FAIL: $name - version endpoint was not checked."
        exit 1
    }
}

assert_old_volume_preserved() {
    local name="$1"

    if ! grep -Fxq "pulsecheck-data-old" "$WORK_DIR/$name.volumes"; then
        echo "FAIL: $name - previous production volume is missing."
        exit 1
    fi
}

assert_candidate_volume_removed() {
    local name="$1"

    if grep -Fxq "pulsecheck-candidate-test-data" "$WORK_DIR/$name.volumes"; then
        echo "FAIL: $name - failed candidate volume was not removed."
        exit 1
    fi
}

assert_candidate_volume_preserved() {
    local name="$1"

    if ! grep -Fxq "pulsecheck-candidate-test-data" "$WORK_DIR/$name.volumes"; then
        echo "FAIL: $name - successful deployment candidate volume is missing."
        exit 1
    fi
}

INITIAL=$'pulsecheck-app\npulsecheck-app-previous\npulsecheck-candidate-test'

echo "===== CASE 1: Candidate fails before old container stops ====="

run_case "candidate-failure" 1 0 0 0 "$INITIAL" 1 1

assert_container_present "candidate-failure" "pulsecheck-app"
assert_container_present "candidate-failure" "pulsecheck-app-previous"
assert_container_absent "candidate-failure" "pulsecheck-candidate-test"
assert_old_volume_preserved "candidate-failure"
assert_candidate_volume_removed "candidate-failure"

if grep -Fq "docker rm -f pulsecheck-app" \
    "$WORK_DIR/candidate-failure.actions"; then
    echo "FAIL: Candidate failure removed the current application."
    exit 1
fi

if grep -q '^curl ' "$WORK_DIR/candidate-failure.actions"; then
    echo "FAIL: Health check unexpectedly ran before the old container stopped."
    exit 1
fi

echo "PASS: Candidate cleanup preserves production and previous container."

echo
echo "===== CASE 2: Old container stopped, switch not started ====="

run_case "old-stopped-failure" 1 1 0 0 "$INITIAL" 1 1

assert_container_present "old-stopped-failure" "pulsecheck-app"
assert_container_present "old-stopped-failure" "pulsecheck-app-previous"
assert_container_absent "old-stopped-failure" "pulsecheck-candidate-test"
assert_start_called "old-stopped-failure"
assert_health_checks "old-stopped-failure"
assert_old_volume_preserved "old-stopped-failure"
assert_candidate_volume_removed "old-stopped-failure"

grep -Fq "PASS: Original production application restarted." \
    "$WORK_DIR/old-stopped-failure.output"

echo "PASS: Original application is restarted and health/version checks succeed."

echo
echo "===== CASE 3: Production switch fails ====="

run_case "switch-failure" 1 1 1 0 "$INITIAL" 1 1

assert_container_present "switch-failure" "pulsecheck-app"
assert_container_absent "switch-failure" "pulsecheck-app-previous"
assert_container_absent "switch-failure" "pulsecheck-candidate-test"
assert_start_called "switch-failure"
assert_health_checks "switch-failure"
assert_old_volume_preserved "switch-failure"
assert_candidate_volume_removed "switch-failure"

grep -Fq \
    "docker rename pulsecheck-app-previous pulsecheck-app" \
    "$WORK_DIR/switch-failure.actions"

grep -Fq "PASS: Previous production application restored." \
    "$WORK_DIR/switch-failure.output"

echo "PASS: Previous container restored and health/version checks succeed."

echo
echo "===== CASE 4: Restored application fails health check ====="

run_case "restore-health-failure" 1 1 1 0 "$INITIAL" 1 0

assert_container_present "restore-health-failure" "pulsecheck-app"
assert_container_absent "restore-health-failure" "pulsecheck-app-previous"
assert_container_absent "restore-health-failure" "pulsecheck-candidate-test"
assert_start_called "restore-health-failure"
if ! grep -Fq 'curl -fsS http://127.0.0.1:8000/health' \
    "$WORK_DIR/restore-health-failure.actions"; then
    echo "FAIL: Failed restoration did not check the health endpoint."
    exit 1
fi

if grep -Fq 'curl -fsS http://127.0.0.1:8000/version' \
    "$WORK_DIR/restore-health-failure.actions"; then
    echo "FAIL: Version endpoint was checked despite health failure."
    exit 1
fi

assert_old_volume_preserved "restore-health-failure"
assert_candidate_volume_removed "restore-health-failure"

grep -Fq "ERROR: Previous application did not recover." \
    "$WORK_DIR/restore-health-failure.output"

if grep -Fq "PASS: Previous production application restored." \
    "$WORK_DIR/restore-health-failure.output"; then
    echo "FAIL: Unhealthy restoration was incorrectly reported as successful."
    exit 1
fi

echo "PASS: Failed health check is detected and reported."

echo
echo "===== CASE 5: Deployment succeeds ====="

SUCCESSFUL=$'pulsecheck-app\npulsecheck-app-previous'

run_case "successful-deployment" 1 1 1 1 "$SUCCESSFUL" 0 1

assert_container_present "successful-deployment" "pulsecheck-app"
assert_container_present "successful-deployment" "pulsecheck-app-previous"
assert_old_volume_preserved "successful-deployment"
assert_candidate_volume_preserved "successful-deployment"

if grep -Fq "docker rm -f pulsecheck-app" \
    "$WORK_DIR/successful-deployment.actions"; then
    echo "FAIL: Successful deployment unexpectedly removed production."
    exit 1
fi

if grep -q '^curl ' "$WORK_DIR/successful-deployment.actions"; then
    echo "FAIL: Rollback health checks ran after successful deployment."
    exit 1
fi

echo "PASS: Successful deployment retains current and rollback containers."

echo
echo "===== CASE 6: Previous-container rename fails during recovery ====="

export MOCK_FAIL_RENAME=1
run_case "restore-rename-failure" 1 1 1 0 "$INITIAL" 1 1
unset MOCK_FAIL_RENAME

assert_container_present "restore-rename-failure" "pulsecheck-app-previous"
assert_container_absent "restore-rename-failure" "pulsecheck-app"
assert_container_absent "restore-rename-failure" "pulsecheck-candidate-test"
assert_candidate_volume_removed "restore-rename-failure"

grep -Fq "ERROR: Previous production container could not be renamed." \
    "$WORK_DIR/restore-rename-failure.output"
grep -Fq "CRITICAL: Automatic recovery was incomplete." \
    "$WORK_DIR/restore-rename-failure.output"

echo "PASS: Rename failure is reported and candidate cleanup still runs."

echo
echo "===== CASE 7: Previous-container start fails during recovery ====="

export MOCK_FAIL_START=1
run_case "restore-start-failure" 1 1 1 0 "$INITIAL" 1 1
unset MOCK_FAIL_START

assert_container_present "restore-start-failure" "pulsecheck-app"
assert_container_absent "restore-start-failure" "pulsecheck-app-previous"
assert_container_absent "restore-start-failure" "pulsecheck-candidate-test"
assert_candidate_volume_removed "restore-start-failure"

grep -Fq "ERROR: Previous production container could not be started." \
    "$WORK_DIR/restore-start-failure.output"
grep -Fq "CRITICAL: Automatic recovery was incomplete." \
    "$WORK_DIR/restore-start-failure.output"

echo "PASS: Start failure is reported and candidate cleanup still runs."

echo
echo "ALL SEVEN MOCKED ROLLBACK REGRESSION SCENARIOS PASSED."
