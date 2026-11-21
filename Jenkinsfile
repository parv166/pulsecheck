
pipeline {
    agent any

    options {
        timestamps()
        disableConcurrentBuilds()
        skipDefaultCheckout(true)
    }

    parameters {
        booleanParam(
            name: 'DEPLOY_TO_AWS',
            defaultValue: false,
            description: 'Enable only when intentionally pushing to ECR and deploying to EC2.'
        )
    }

    stages {

        stage('Checkout') {
            steps {
                checkout scm
                echo 'Source code checkout completed'
            }
        }

        stage('Run Unit Tests') {
            steps {
                sh '''
                    docker run --rm \
                      -e PYTHONDONTWRITEBYTECODE=1 \
                      -v "$WORKSPACE:/workspace" \
                      -w /workspace \
                      python:3.14-slim \
                      sh -c "python -m pip install --no-cache-dir -r requirements-dev.txt && python -m pytest -v -p no:cacheprovider"
                '''
            }
        }

        stage('Build Docker Image') {
            steps {
                withCredentials([
                    usernamePassword(
                        credentialsId: 'dhi-registry-readonly',
                        usernameVariable: 'DHI_USERNAME',
                        passwordVariable: 'DHI_PASSWORD'
                    )
                ]) {
                    sh '''
                        set +x
                        set -eu

                        docker_config="$(mktemp -d)"
                        trap 'rm -rf "$docker_config"' EXIT
                        export DOCKER_CONFIG="$docker_config"

                        echo "===== Authenticating to DHI registry ====="

                        printf '%s' "$DHI_PASSWORD" | docker login dhi.io \
                          --username "$DHI_USERNAME" \
                          --password-stdin

                        echo "===== Building PulseCheck using DHI ====="

                        docker buildx build \
                          --pull \
                          --platform linux/amd64 \
                          --provenance=mode=max \
                          --sbom=true \
                          --load \
                          --progress=plain \
                          -f Dockerfile.dhi \
                          -t pulsecheck:${BUILD_NUMBER} \
                          .
                    '''
                }
            }
        }

        stage('Trivy Vulnerability Scan') {
            steps {
                withCredentials([
                    usernamePassword(
                        credentialsId: 'dhi-registry-readonly',
                        usernameVariable: 'DHI_USERNAME',
                        passwordVariable: 'DHI_PASSWORD'
                    )
                ]) {
                    sh '''
                        set +x
                        set -eu

                        image="pulsecheck:${BUILD_NUMBER}"

                        docker_config="$(mktemp -d)"
                        trap 'rm -rf "$docker_config"' EXIT
                        export DOCKER_CONFIG="$docker_config"

                        echo "===== Authenticating Docker Hub ====="

                        printf '%s' "$DHI_PASSWORD" | docker login \
                          --username "$DHI_USERNAME" \
                          --password-stdin

                        echo "===== Authenticating DHI registry ====="

                        printf '%s' "$DHI_PASSWORD" | docker login dhi.io \
                          --username "$DHI_USERNAME" \
                          --password-stdin

                        echo "===== Retrieving and verifying DHI VEX ====="

                        docker scout vex get \
                          --verify \
                          --skip-tlog \
                          --output dhi-python-vex.json \
                          registry://dhi.io/python:3.14

                        test -s dhi-python-vex.json
                        test -s pulsecheck-vex.json

                        echo "===== Raw Trivy vulnerability scan ====="

                        trivy image \
                          --scanners vuln \
                          --severity HIGH,CRITICAL \
                          --format table \
                          --exit-code 0 \
                          "$image"

                        trivy image \
                          --scanners vuln \
                          --format json \
                          --output trivy-image-report.json \
                          --exit-code 0 \
                          "$image"

                        echo "===== Trivy scan with DHI VEX applied ====="

                        trivy image \
                          --scanners vuln \
                          --vex dhi-python-vex.json \
                          --vex pulsecheck-vex.json \
                          --show-suppressed \
                          --severity HIGH,CRITICAL \
                          --format table \
                          --exit-code 0 \
                          "$image"

                        trivy image \
                          --scanners vuln \
                          --vex dhi-python-vex.json \
                          --vex pulsecheck-vex.json \
                          --show-suppressed \
                          --format json \
                          --output trivy-image-vex-report.json \
                          --exit-code 0 \
                          "$image"
                    '''
                }

                archiveArtifacts artifacts: 'trivy-image-report.json,trivy-image-vex-report.json,dhi-python-vex.json,pulsecheck-vex.json',
                                 fingerprint: true

                sh '''
                    set -eu

                    image="pulsecheck:${BUILD_NUMBER}"

                    echo "===== VEX-aware HIGH/CRITICAL Quality Gate ====="

                    test -s dhi-python-vex.json
                    test -s pulsecheck-vex.json

                    trivy image \
                      --scanners vuln \
                      --vex dhi-python-vex.json \
                      --vex pulsecheck-vex.json \
                      --severity HIGH,CRITICAL \
                      --format table \
                      --exit-code 1 \
                      "$image"
                '''
            }
        }

        stage('Container Smoke Test') {
            steps {
                sh '''
                    set -eu

                    container="pulsecheck-ci-${BUILD_NUMBER}"
                    volume="${container}-data"
                    host_port=$((18000 + BUILD_NUMBER))

                    cleanup() {
                        docker rm -f "$container" >/dev/null 2>&1 || true
                        docker volume rm "$volume" >/dev/null 2>&1 || true
                    }

                    trap cleanup EXIT

                    docker volume create "$volume" >/dev/null

                    echo "Starting hardened DHI PulseCheck container..."

                    docker run -d \
                      --name "$container" \
                      --read-only \
                      --cap-drop=ALL \
                      --security-opt=no-new-privileges:true \
                      --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \
                      --pids-limit=100 \
                      --mount "source=$volume,target=/app/data" \
                      -p "127.0.0.1:${host_port}:8000" \
                      -e APP_VERSION="ci-${BUILD_NUMBER}" \
                      "pulsecheck:${BUILD_NUMBER}"

                    ready=0

                    echo "Waiting for application health..."

                    for attempt in $(seq 1 30); do
                        if curl -fsS "http://127.0.0.1:${host_port}/health"; then
                            echo
                            ready=1
                            break
                        fi

                        sleep 2
                    done

                    if [ "$ready" -ne 1 ]; then
                        docker logs "$container"
                        echo "FAIL: Container health test failed"
                        exit 1
                    fi

                    echo "Checking non-root user and persistent SQLite storage..."

                    docker exec "$container" /app/venv/bin/python -c '
import os
import sqlite3
import tempfile

data_dir = "/app/data"
db_path = os.path.join(data_dir, "pulsecheck.db")

assert os.geteuid() == 65532, f"Unexpected UID: {os.geteuid()}"
assert os.path.isdir(data_dir), "Data directory is missing"
assert os.access(data_dir, os.W_OK), "Data directory is not writable"
assert os.path.isfile(db_path), "SQLite database is missing"

with tempfile.NamedTemporaryFile(
    prefix=".ci-write-test-",
    dir=data_dir,
    delete=True
) as test_file:
    test_file.write(b"PulseCheck CI storage test")
    test_file.flush()

with sqlite3.connect(db_path, timeout=5) as db:
    assert db.execute("SELECT 1").fetchone() == (1,)

print("PASS: Container runs as UID 65532")
print("PASS: Persistent data directory is writable")
print("PASS: SQLite database exists and is accessible")
'

                    echo "Hardened DHI container smoke test successful"
                '''
            }
        }

        stage('End-to-End Monitoring Integration Test') {
            steps {
                sh '''
                    set -eu
                    echo "Running isolated UP -> DOWN -> UP integration test"
                    PULSECHECK_IMAGE="pulsecheck:${BUILD_NUMBER}" bash scripts/run-e2e-ci.sh
                '''
            }
        }

        stage('Deployment Rollback Regression Test') {
            steps {
                sh '''
                    set -eu
                    bash -n scripts/test-deploy-rollback.sh
                    bash scripts/test-deploy-rollback.sh
                '''
            }
        }

        stage('Real Docker Deployment Rollback Test') {
            steps {
                sh '''
                    set -eu
                    echo "Testing real Docker recovery and SQLite preservation"
                    bash -n scripts/test-deploy-rollback-real.sh
                    PULSECHECK_IMAGE="pulsecheck:${BUILD_NUMBER}" bash scripts/test-deploy-rollback-real.sh
                '''
            }
        }

        stage('SQLite Volume Migration Regression Test') {
            steps {
                sh '''
                    set -eu
                    echo "Testing SQLite migration, data equality, and volume permissions"
                    bash -n scripts/test-sqlite-volume-migration.sh
                    PULSECHECK_IMAGE="pulsecheck:${BUILD_NUMBER}" bash scripts/test-sqlite-volume-migration.sh
                '''
            }
        }

        stage('Push Image to Amazon ECR') {
            when {
                expression { params.DEPLOY_TO_AWS }
            }
            steps {
                withCredentials([usernamePassword(
                    credentialsId: 'aws-ecr-push',
                    usernameVariable: 'AWS_ACCESS_KEY_ID',
                    passwordVariable: 'AWS_SECRET_ACCESS_KEY'
                )]) {
                    sh '''#!/bin/bash
                        set -euo pipefail
                        set +x

                        export AWS_DEFAULT_REGION=ap-south-1

                        echo "Creating temporary Docker configuration..."

                        DOCKER_CONFIG_DIR=$(mktemp -d /tmp/pulsecheck-ecr-push.XXXXXX)
                        export DOCKER_CONFIG="$DOCKER_CONFIG_DIR"

                        cleanup() {
                            rm -rf "$DOCKER_CONFIG_DIR"
                        }

                        trap cleanup EXIT

                        account_id=$(/usr/local/bin/aws sts get-caller-identity \
                          --query Account \
                          --output text)

                        registry="${account_id}.dkr.ecr.${AWS_DEFAULT_REGION}.amazonaws.com"

                        image="pulsecheck:${BUILD_NUMBER}"
                        ecr_image="${registry}/pulsecheck:${BUILD_NUMBER}"

                        echo "Authenticating with Amazon ECR..."

                        /usr/local/bin/aws ecr get-login-password \
                          --region "$AWS_DEFAULT_REGION" \
                        | docker login \
                          --username AWS \
                          --password-stdin "$registry"

                        echo "Pushing image: $ecr_image"

                        docker tag "$image" "$ecr_image"

                        docker push "$ecr_image"

                        echo "Image successfully pushed to ECR."
                        echo "Temporary Docker credentials will now be removed."
                    '''
                }
            }
        }

        stage('Deploy to EC2') {
            when {
                expression { params.DEPLOY_TO_AWS }
            }
            steps {
                withCredentials([
                    sshUserPrivateKey(
                        credentialsId: 'pulsecheck-ec2-ssh',
                        keyFileVariable: 'EC2_SSH_KEY',
                        usernameVariable: 'EC2_SSH_USER'
                    ),
                    file(
                        credentialsId: 'pulsecheck-ec2-known-hosts',
                        variable: 'EC2_KNOWN_HOSTS'
                    )
                ]) {
                    sh '''#!/bin/bash
                        set -euo pipefail
                        set +x

                        EC2_HOST="16.4.95.134"

                        echo "Deploying build ${BUILD_NUMBER} to EC2..."

                        REMOTE_ASSET_DIR=""
                        SSH_OPTIONS=(
                          -i "$EC2_SSH_KEY"
                          -o BatchMode=yes
                          -o IdentitiesOnly=yes
                          -o StrictHostKeyChecking=yes
                          -o UserKnownHostsFile="$EC2_KNOWN_HOSTS"
                          -o ConnectTimeout=15
                        )

                        cleanup_local_remote_assets() {
                            local status=$?
                            trap - EXIT

                            if [[ -n "$REMOTE_ASSET_DIR" ]]; then
                                if [[ "$REMOTE_ASSET_DIR" =~ ^/tmp/pulsecheck-metrics-${BUILD_NUMBER}\.[A-Za-z0-9]+$ ]]; then
                                    ssh "${SSH_OPTIONS[@]}" "$EC2_SSH_USER@$EC2_HOST" \
                                      "rm -rf -- '$REMOTE_ASSET_DIR'" || \
                                      echo "WARNING: Could not remove remote deployment assets: $REMOTE_ASSET_DIR"
                                else
                                    echo "WARNING: Refusing cleanup of an unexpected remote asset path."
                                fi
                            fi

                            exit "$status"
                        }
                        trap cleanup_local_remote_assets EXIT

                        echo "===== VALIDATING LOCAL DEPLOYMENT ASSETS ====="

                        for asset in \
                          collect_ec2_metrics.py \
                          install_ec2_metrics.sh \
                          validate_metrics_snapshot.py \
                          backup_sqlite_volume.py; do
                            test -s "$WORKSPACE/scripts/$asset"
                        done

                        for asset in pulsecheck-metrics.service pulsecheck-metrics.timer; do
                            test -s "$WORKSPACE/deploy/systemd/$asset"
                        done

                        echo "===== CREATING PRIVATE REMOTE ASSET DIRECTORY ====="

                        REMOTE_ASSET_DIR="$(ssh "${SSH_OPTIONS[@]}" "$EC2_SSH_USER@$EC2_HOST" \
                          "umask 077 && mktemp -d '/tmp/pulsecheck-metrics-${BUILD_NUMBER}.XXXXXX'")"

                        if [[ ! "$REMOTE_ASSET_DIR" =~ ^/tmp/pulsecheck-metrics-${BUILD_NUMBER}\.[A-Za-z0-9]+$ ]]; then
                            echo "ERROR: EC2 returned an unexpected temporary asset path."
                            exit 1
                        fi

                        echo "Remote asset directory created."

                        echo "===== TRANSFERRING DEPLOYMENT ASSETS ====="

                        tar -cf - \
                          -C "$WORKSPACE/scripts" collect_ec2_metrics.py install_ec2_metrics.sh validate_metrics_snapshot.py backup_sqlite_volume.py \
                          -C "$WORKSPACE/deploy/systemd" pulsecheck-metrics.service pulsecheck-metrics.timer \
                          | ssh "${SSH_OPTIONS[@]}" "$EC2_SSH_USER@$EC2_HOST" \
                              "test -d '$REMOTE_ASSET_DIR' && test \"\$(stat -c '%a' '$REMOTE_ASSET_DIR')\" = 700 && tar -xf - -C '$REMOTE_ASSET_DIR'"

                        ssh "${SSH_OPTIONS[@]}" \
                          "$EC2_SSH_USER@$EC2_HOST" \
                          "bash -s -- '${BUILD_NUMBER}' '${REMOTE_ASSET_DIR}'" <<'REMOTE_SCRIPT'
set -Eeuo pipefail

BUILD_TAG="$1"
REMOTE_ASSET_DIR="${2:-}"
AWS_REGION="ap-south-1"

if [[ ! "$BUILD_TAG" =~ ^[0-9]+$ ]]; then
    echo "ERROR: Invalid Jenkins build number."
    exit 1
fi

REMOTE_ASSET_PATTERN="^/tmp/pulsecheck-metrics-${BUILD_TAG}\.[A-Za-z0-9]+$"

if [[ ! "$REMOTE_ASSET_DIR" =~ $REMOTE_ASSET_PATTERN ]]; then
    echo "ERROR: Remote asset directory does not match this deployment."
    exit 1
fi

if [[ ! -d "$REMOTE_ASSET_DIR" || -L "$REMOTE_ASSET_DIR" ]]; then
    echo "ERROR: Remote deployment asset directory is missing or unsafe."
    exit 1
fi

REMOTE_ASSET_OWNER_MODE=$(stat -c '%u:%a' "$REMOTE_ASSET_DIR")

if [[ "$REMOTE_ASSET_OWNER_MODE" != "$(id -u):700" ]]; then
    echo "ERROR: Remote asset directory must be owned by the SSH user with mode 700."
    exit 1
fi

cleanup_remote_assets() {
    if [[ "$REMOTE_ASSET_DIR" =~ $REMOTE_ASSET_PATTERN ]] \
      && [[ -d "$REMOTE_ASSET_DIR" && ! -L "$REMOTE_ASSET_DIR" ]] \
      && [[ "$(stat -c '%u' "$REMOTE_ASSET_DIR" 2>/dev/null)" == "$(id -u)" ]]; then
        rm -rf -- "$REMOTE_ASSET_DIR" || \
            echo "WARNING: Remote deployment asset cleanup failed."
    else
        echo "WARNING: Refusing cleanup of an unexpected remote asset path."
    fi
}
trap cleanup_remote_assets EXIT

echo "===== VERIFYING TRANSFERRED DEPLOYMENT ASSETS ====="

for asset in \
    collect_ec2_metrics.py \
    install_ec2_metrics.sh \
    validate_metrics_snapshot.py \
    backup_sqlite_volume.py \
    pulsecheck-metrics.service \
    pulsecheck-metrics.timer; do

    if [[ ! -f "$REMOTE_ASSET_DIR/$asset" \
       || -L "$REMOTE_ASSET_DIR/$asset" \
       || ! -s "$REMOTE_ASSET_DIR/$asset" ]]; then
        echo "ERROR: Required deployment asset is missing or invalid: $asset"
        exit 1
    fi
done

echo "PASS: All six deployment assets are present and non-empty."

APP_CONTAINER="pulsecheck-app"
PREVIOUS_CONTAINER="pulsecheck-app-previous"
CANDIDATE_CONTAINER="pulsecheck-candidate-${BUILD_TAG}"

CANDIDATE_VOLUME="pulsecheck-data-${BUILD_TAG}"
CANDIDATE_PORT=18081

DEPLOYMENT_COMPLETE=0
OLD_CONTAINER_EXISTS=0
OLD_CONTAINER_STOPPED=0
PRODUCTION_SWITCH_STARTED=0
CANDIDATE_VOLUME_CREATED=0
CANDIDATE_CONTAINER_CREATED=0

OLD_VOLUME=""
DOCKER_CONFIG_DIR=""

# Validate production Discord notification secret before deployment.
SECRET_ENV_FILE="/etc/pulsecheck-secrets/pulsecheck.env"

echo "===== VALIDATING PRODUCTION DISCORD SECRET ====="

if ! sudo test -s "$SECRET_ENV_FILE"; then
    echo "ERROR: Production Discord secret file is missing or empty."
    exit 1
fi

SECRET_OWNER_MODE=$(sudo stat -c '%U:%a' "$SECRET_ENV_FILE")

if [ "$SECRET_OWNER_MODE" != "root:600" ]; then
    echo "ERROR: Discord secret file must be owned by root with mode 600."
    exit 1
fi

if ! sudo grep -Eq '^PULSECHECK_WEBHOOK_URL=https://discord[.]com/api/webhooks/[0-9]+/[^[:space:]]+$' "$SECRET_ENV_FILE"; then
    echo "ERROR: Discord webhook configuration is invalid."
    exit 1
fi

if ! sudo grep -Eq '^PULSECHECK_ADMIN_USERNAME=[^[:space:]]+$' "$SECRET_ENV_FILE"; then
    echo "ERROR: Production admin username is missing or invalid."
    exit 1
fi

if ! sudo grep -Eq '^PULSECHECK_ADMIN_PASSWORD_HASH=scrypt-v1[$][0-9a-f]{32}[$][0-9a-f]{128}$' "$SECRET_ENV_FILE"; then
    echo "ERROR: Production admin password hash is missing or invalid."
    exit 1
fi

if ! sudo grep -Eq '^PULSECHECK_COOKIE_SECURE=true$' "$SECRET_ENV_FILE"; then
    echo "ERROR: Production session cookies must use Secure."
    exit 1
fi

echo "PASS: Production Discord and admin authentication configuration validated (values hidden)."

cleanup_ecr_credentials() {
    if [ -n "${DOCKER_CONFIG_DIR:-}" ]; then
        sudo rm -rf "$DOCKER_CONFIG_DIR" || true
        DOCKER_CONFIG_DIR=""
    fi
}

wait_for_app() {
    local port="$1"

    for attempt in $(seq 1 30); do
        if curl -fsS "http://127.0.0.1:${port}/health" >/dev/null \
          && curl -fsS "http://127.0.0.1:${port}/version" >/dev/null; then
            return 0
        fi

        sleep 2
    done

    return 1
}


verify_metrics_mount() {
    local container="$1"
    local mount_rw

    mount_rw=$(sudo docker inspect \
      --format '{{range .Mounts}}{{if eq .Destination "/run/pulsecheck-metrics"}}{{.RW}}{{end}}{{end}}' \
      "$container")

    if [ "$mount_rw" != "false" ]; then
        echo "ERROR: Metrics snapshot mount is missing or writable in $container."
        return 1
    fi

    sudo docker exec "$container" /app/venv/bin/python -c '
import json
import os
from pathlib import Path

assert os.geteuid() == 65532, "Unexpected application UID"

path = Path("/run/pulsecheck-metrics/metrics.json")
assert path.is_file(), "Metrics snapshot is missing"

with path.open(encoding="utf-8") as handle:
    snapshot = json.load(handle)

assert snapshot.get("schema_version") == 1

print("PASS: Metrics snapshot readable by application UID 65532.")
print("PASS: Snapshot schema verified.")
'
}

rollback_and_cleanup() {
    local exit_status=$?
    local recovery_failed=0
    trap - EXIT

    # Recovery errors must not prevent candidate cleanup.
    set +e
    cleanup_ecr_credentials
    cleanup_remote_assets

    if [ "$DEPLOYMENT_COMPLETE" -ne 1 ]; then
        echo "===== DEPLOYMENT FAILED: RECOVERY ====="

        if [ "$PRODUCTION_SWITCH_STARTED" -eq 1 ]; then
            if [ "$OLD_CONTAINER_EXISTS" -eq 1 ]; then
                if sudo docker inspect "$PREVIOUS_CONTAINER" >/dev/null 2>&1; then
                    echo "Restoring previous production container..."
                    sudo docker rm -f "$APP_CONTAINER" >/dev/null 2>&1 || true

                    if sudo docker inspect "$APP_CONTAINER" >/dev/null 2>&1; then
                        echo "ERROR: Candidate application container could not be removed."
                        recovery_failed=1
                    elif sudo docker rename "$PREVIOUS_CONTAINER" "$APP_CONTAINER"; then
                        if sudo docker start "$APP_CONTAINER" >/dev/null; then
                            if wait_for_app 8000; then
                                echo "PASS: Previous production application restored."
                            else
                                echo "ERROR: Previous application did not recover."
                                sudo docker logs "$APP_CONTAINER" || true
                                recovery_failed=1
                            fi
                        else
                            echo "ERROR: Previous production container could not be started."
                            sudo docker logs "$APP_CONTAINER" || true
                            recovery_failed=1
                        fi
                    else
                        echo "ERROR: Previous production container could not be renamed."
                        recovery_failed=1
                    fi

                elif sudo docker inspect "$APP_CONTAINER" >/dev/null 2>&1; then
                    echo "Previous container rename did not complete."
                    echo "Restarting the original application..."

                    if sudo docker start "$APP_CONTAINER" >/dev/null; then
                        if wait_for_app 8000; then
                            echo "PASS: Original production application restarted."
                        else
                            echo "ERROR: Original application did not recover."
                            sudo docker logs "$APP_CONTAINER" || true
                            recovery_failed=1
                        fi
                    else
                        echo "ERROR: Original production application could not be started."
                        recovery_failed=1
                    fi
                else
                    echo "ERROR: Could not find the original production container."
                    recovery_failed=1
                fi
            else
                echo "No previous production container existed."
                sudo docker rm -f "$APP_CONTAINER" >/dev/null 2>&1 || true
            fi

        elif [ "$OLD_CONTAINER_STOPPED" -eq 1 ] \
          && [ "$OLD_CONTAINER_EXISTS" -eq 1 ]; then
            echo "Restarting original application after candidate failure..."

            if sudo docker inspect "$APP_CONTAINER" >/dev/null 2>&1; then
                if sudo docker start "$APP_CONTAINER" >/dev/null; then
                    if wait_for_app 8000; then
                        echo "PASS: Original production application restarted."
                    else
                        echo "ERROR: Original application did not recover."
                        sudo docker logs "$APP_CONTAINER" || true
                        recovery_failed=1
                    fi
                else
                    echo "ERROR: Original production application could not be started."
                    recovery_failed=1
                fi
            else
                echo "ERROR: Original production container is missing."
                recovery_failed=1
            fi
        fi

        echo "Removing failed candidate resources..."

        if [ "$CANDIDATE_CONTAINER_CREATED" -eq 1 ]; then
            sudo docker stop -t 15 "$CANDIDATE_CONTAINER" >/dev/null 2>&1 || true

            if sudo docker rm -f "$CANDIDATE_CONTAINER" >/dev/null 2>&1; then
                CANDIDATE_CONTAINER_CREATED=0
            elif ! sudo docker inspect "$CANDIDATE_CONTAINER" >/dev/null 2>&1; then
                CANDIDATE_CONTAINER_CREATED=0
            else
                echo "ERROR: Failed candidate container could not be removed."
                recovery_failed=1
            fi
        fi

        if [ "$CANDIDATE_VOLUME_CREATED" -eq 1 ]; then
            if sudo docker volume rm "$CANDIDATE_VOLUME" >/dev/null 2>&1; then
                CANDIDATE_VOLUME_CREATED=0
            elif ! sudo docker volume inspect "$CANDIDATE_VOLUME" >/dev/null 2>&1; then
                CANDIDATE_VOLUME_CREATED=0
            else
                echo "ERROR: Failed candidate volume could not be removed."
                recovery_failed=1
            fi
        fi

        if [ "$recovery_failed" -eq 1 ]; then
            echo "CRITICAL: Automatic recovery was incomplete. Manual EC2 inspection is required."
        fi
    fi

    exit "$exit_status"
}
trap rollback_and_cleanup EXIT

if sudo docker inspect "$APP_CONTAINER" >/dev/null 2>&1; then
    OLD_CONTAINER_EXISTS=1
fi

# A running legacy container does not necessarily have persistent SQLite data.
# Detect the actual /app/data mount independently of container existence.
OLD_DATA_VOLUME_EXISTS=0

if [ "$OLD_CONTAINER_EXISTS" -eq 1 ]; then
    OLD_DATA_MOUNT_TYPE=$(sudo docker inspect \
      --format '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Type}}{{end}}{{end}}' \
      "$APP_CONTAINER")

    case "$OLD_DATA_MOUNT_TYPE" in
        volume)
            OLD_VOLUME=$(sudo docker inspect \
              --format '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{if eq .Type "volume"}}{{.Name}}{{end}}{{end}}{{end}}' \
              "$APP_CONTAINER")

            if [ -z "$OLD_VOLUME" ]; then
                echo "ERROR: /app/data is mounted as a volume but its name could not be identified."
                exit 1
            fi

            OLD_DATA_VOLUME_EXISTS=1
            ;;
        bind)
            echo "ERROR: Existing production uses a bind mount at /app/data."
            echo "Manual inspection is required before migrating this deployment."
            exit 1
            ;;
        "")
            echo "Legacy stateless production container detected: no /app/data mount."
            ;;
        *)
            echo "ERROR: Unsupported /app/data mount type: $OLD_DATA_MOUNT_TYPE"
            exit 1
            ;;
    esac
fi

if [ "$OLD_CONTAINER_EXISTS" -eq 0 ] \
  && sudo docker inspect "$PREVIOUS_CONTAINER" >/dev/null 2>&1; then

    echo "ERROR: Current application is missing but a previous container exists."
    echo "Refusing to deploy without identifying the active production database."
    exit 1
fi

if sudo docker inspect "$CANDIDATE_CONTAINER" >/dev/null 2>&1; then
    echo "ERROR: Candidate container already exists: $CANDIDATE_CONTAINER"
    echo "Refusing to remove or reuse a pre-existing container."
    exit 1
fi

echo "===== AUTHENTICATING WITH AMAZON ECR ====="

ACCOUNT_ID=$(aws sts get-caller-identity \
  --region "$AWS_REGION" \
  --query Account \
  --output text)

REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
IMAGE="${REGISTRY}/pulsecheck:${BUILD_TAG}"

DOCKER_CONFIG_DIR=$(sudo mktemp -d /tmp/pulsecheck-ecr.XXXXXX)

aws ecr get-login-password --region "$AWS_REGION" \
  | sudo env DOCKER_CONFIG="$DOCKER_CONFIG_DIR" \
      docker login \
      --username AWS \
      --password-stdin "$REGISTRY"

sudo env DOCKER_CONFIG="$DOCKER_CONFIG_DIR" docker pull "$IMAGE"

cleanup_ecr_credentials

echo "===== PREPARING NEW VERSIONED DATA VOLUME ====="

if sudo docker volume inspect "$CANDIDATE_VOLUME" >/dev/null 2>&1; then
    echo "ERROR: Candidate volume already exists: $CANDIDATE_VOLUME"
    echo "Refusing to overwrite an existing volume."
    exit 1
fi

sudo docker volume create "$CANDIDATE_VOLUME" >/dev/null
CANDIDATE_VOLUME_CREATED=1

if [ "$OLD_DATA_VOLUME_EXISTS" -eq 1 ]; then

    echo "===== IDENTIFYING CURRENT PRODUCTION VOLUME ====="

    OLD_VOLUME=$(sudo docker inspect \
      --format '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{if eq .Type "volume"}}{{.Name}}{{end}}{{end}}{{end}}' \
      "$APP_CONTAINER")

    if [ -z "$OLD_VOLUME" ]; then
        echo "ERROR: Could not identify the current production Docker volume."
        exit 1
    fi

    if ! sudo docker volume inspect "$OLD_VOLUME" >/dev/null 2>&1; then
        echo "ERROR: Original production volume does not exist."
        exit 1
    fi

    if [ "$OLD_VOLUME" = "$CANDIDATE_VOLUME" ]; then
        echo "ERROR: Source and destination volumes must be different."
        exit 1
    fi

    echo "Original production volume: $OLD_VOLUME"
    echo "New DHI production volume: $CANDIDATE_VOLUME"

    echo "===== GRACEFULLY STOPPING OLD APPLICATION ====="

    sudo docker stop -t 30 "$APP_CONTAINER" >/dev/null
    OLD_CONTAINER_STOPPED=1

    echo "===== CREATING CONSISTENT SQLITE BACKUP ====="

    sudo docker run --rm \
      --user 0:0 \
      --mount "type=volume,source=$OLD_VOLUME,target=/source" \
      --mount "type=volume,source=$CANDIDATE_VOLUME,target=/dest" \
      --mount "type=bind,source=$REMOTE_ASSET_DIR/backup_sqlite_volume.py,target=/backup_sqlite_volume.py,readonly" \
      --entrypoint /app/venv/bin/python \
      "$IMAGE" \
      /backup_sqlite_volume.py \
      --source /source \
      --destination /dest

else
    echo "No existing production data volume detected."

    for legacy_volume in pulsecheck-data pulsecheck_pulsecheck_data; do
        if sudo docker volume inspect "$legacy_volume" >/dev/null 2>&1; then
            echo "ERROR: Existing legacy PulseCheck volume found: $legacy_volume"
            echo "Refusing to silently initialize an empty production database."
            exit 1
        fi
    done

    echo "===== INITIALIZING NEW DATA VOLUME ====="
    echo "First deployment or legacy stateless upgrade."

    sudo docker run --rm \
      --user 0:0 \
      --mount "type=volume,source=$CANDIDATE_VOLUME,target=/app/data" \
      --entrypoint /app/venv/bin/python \
      "$IMAGE" \
      -c '
import os
os.chown("/app/data", 65532, 65532)
os.chmod("/app/data", 0o750)
print("Initial DHI data volume permissions prepared.")
'
fi

echo "===== INSTALLING EC2 METRICS COLLECTOR ====="

bash "$REMOTE_ASSET_DIR/install_ec2_metrics.sh" "$BUILD_TAG" "$REMOTE_ASSET_DIR"

sudo test -s /var/lib/pulsecheck-metrics/metrics.json

echo "===== TESTING DHI CANDIDATE ====="

sudo docker create \
  --name "$CANDIDATE_CONTAINER" \
  --read-only \
  --cap-drop=ALL \
  --security-opt=no-new-privileges:true \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \
  --pids-limit=100 \
  --mount "type=volume,source=$CANDIDATE_VOLUME,target=/app/data" \
  --mount "type=bind,source=/var/lib/pulsecheck-metrics,target=/run/pulsecheck-metrics,readonly" \
  -p "127.0.0.1:${CANDIDATE_PORT}:8000" \
  -e APP_VERSION="$BUILD_TAG" \
  -e PULSECHECK_DB_PATH=/app/data/pulsecheck.db \
  "$IMAGE" >/dev/null
CANDIDATE_CONTAINER_CREATED=1

sudo docker start "$CANDIDATE_CONTAINER" >/dev/null

if ! wait_for_app "$CANDIDATE_PORT"; then
    echo "ERROR: Candidate health or API checks failed."
    sudo docker logs "$CANDIDATE_CONTAINER" || true
    exit 1
fi

echo "===== VERIFYING CANDIDATE METRICS MOUNT ====="
verify_metrics_mount "$CANDIDATE_CONTAINER"

echo "===== VERIFYING CANDIDATE DATABASE ====="

sudo docker exec "$CANDIDATE_CONTAINER" \
  /app/venv/bin/python -c '
import os
import sqlite3
import tempfile

directory = "/app/data"
database = os.path.join(directory, "pulsecheck.db")

assert os.geteuid() == 65532, f"Unexpected UID: {os.geteuid()}"
assert os.access(directory, os.W_OK), "Data directory is not writable"
assert os.path.isfile(database), "SQLite database is missing"
assert os.stat(database).st_uid == 65532, "Unexpected database owner"

with sqlite3.connect(database, timeout=10) as db:
    integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"SQLite integrity check failed: {integrity}")

    targets = db.execute(
        "SELECT COUNT(*) FROM monitored_targets"
    ).fetchone()[0]

    history = db.execute(
        "SELECT COUNT(*) FROM monitoring_history"
    ).fetchone()[0]

with tempfile.NamedTemporaryFile(
    prefix=".pulsecheck-ci-",
    dir=directory,
    delete=True
) as test_file:
    test_file.write(b"DHI storage verification")
    test_file.flush()

print("PASS: Runtime UID 65532")
print("PASS: Database owner UID 65532")
print("PASS: SQLite integrity check")
print("PASS: Persistent volume writable")
print("Monitored targets:", targets)
print("Monitoring history records:", history)
'

echo "Candidate passed. Preparing production switch."

sudo docker stop -t 15 "$CANDIDATE_CONTAINER" >/dev/null
sudo docker rm "$CANDIDATE_CONTAINER" >/dev/null
CANDIDATE_CONTAINER_CREATED=0

echo "===== PREPARING PRODUCTION SWITCH ====="

if [ "$OLD_CONTAINER_EXISTS" -eq 1 ]; then

    if sudo docker inspect "$PREVIOUS_CONTAINER" >/dev/null 2>&1; then
        echo "Removing older rollback container after candidate validation."
        sudo docker rm -f "$PREVIOUS_CONTAINER" >/dev/null
    fi

fi

# Stop the old container to release production port 8000.
if [ "$OLD_CONTAINER_EXISTS" -eq 1 ] \
  && [ "$OLD_CONTAINER_STOPPED" -eq 0 ]; then

    echo "===== GRACEFULLY STOPPING OLD PRODUCTION CONTAINER ====="

    sudo docker stop -t 30 "$APP_CONTAINER" >/dev/null
    OLD_CONTAINER_STOPPED=1
fi

PRODUCTION_SWITCH_STARTED=1

if [ "$OLD_CONTAINER_EXISTS" -eq 1 ]; then
    echo "Preserving old production container for rollback."
    sudo docker rename "$APP_CONTAINER" "$PREVIOUS_CONTAINER"
fi

echo "===== STARTING NEW DHI PRODUCTION CONTAINER ====="

sudo docker run -d \
  --name "$APP_CONTAINER" \
  --restart unless-stopped \
  --read-only \
  --cap-drop=ALL \
  --security-opt=no-new-privileges:true \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \
  --pids-limit=100 \
  --mount "type=volume,source=$CANDIDATE_VOLUME,target=/app/data" \
  --mount "type=bind,source=/var/lib/pulsecheck-metrics,target=/run/pulsecheck-metrics,readonly" \
  -p 127.0.0.1:8000:8000 \
  --env-file "$SECRET_ENV_FILE" \
  -e APP_VERSION="$BUILD_TAG" \
  -e PULSECHECK_DB_PATH=/app/data/pulsecheck.db \
  "$IMAGE"

echo "===== VERIFYING NEW PRODUCTION APPLICATION ====="

if ! wait_for_app 8000; then
    echo "ERROR: New production application failed health or API checks."
    sudo docker logs "$APP_CONTAINER" || true
    exit 1
fi

echo "===== VERIFYING PRODUCTION METRICS MOUNT ====="
verify_metrics_mount "$APP_CONTAINER"

sudo docker exec "$APP_CONTAINER" \
  /app/venv/bin/python -c '
import os
import sqlite3

path = "/app/data/pulsecheck.db"

assert os.geteuid() == 65532

with sqlite3.connect(path, timeout=10) as db:
    integrity = db.execute("PRAGMA integrity_check").fetchone()[0]

    if integrity != "ok":
        raise RuntimeError(f"Production SQLite integrity check failed: {integrity}")

print("PASS: Production SQLite database integrity verified.")
'

DEPLOYMENT_COMPLETE=1

echo "===== DEPLOYMENT SUCCESSFUL ====="
echo "Deployed version: $BUILD_TAG"
echo "Production volume: $CANDIDATE_VOLUME"

if [ "$OLD_CONTAINER_EXISTS" -eq 1 ]; then
    echo "Previous container retained: $PREVIOUS_CONTAINER"
fi

if [ "$OLD_DATA_VOLUME_EXISTS" -eq 1 ]; then
    echo "Previous data volume retained: $OLD_VOLUME"
    echo "The previous production data volume was not modified."
else
    echo "No previous /app/data Docker volume existed."
    echo "A new empty SQLite database was initialized for this deployment."
fi

REMOTE_SCRIPT
                    '''
                }
            }
        }

    }

    post {
        success {
            echo 'PulseCheck CI pipeline completed successfully.'
        }

        failure {
            echo 'Pipeline failed. Check the stage logs.'
        }
    }
}
