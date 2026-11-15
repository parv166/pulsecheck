
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
                sh '''
                    docker build --pull --no-cache \
                      -t pulsecheck:${BUILD_NUMBER} .
                '''
            }
        }

        stage('Trivy Vulnerability Scan') {
            steps {
                sh '''
                    set -eu

                    image="pulsecheck:${BUILD_NUMBER}"

                    echo "===== Trivy Vulnerability Report ====="

                    trivy image \
                      --scanners vuln \
                      --severity HIGH,CRITICAL \
                      --format table \
                      --exit-code 0 \
                      "$image"

                    echo "===== Saving JSON Security Report ====="

                    trivy image \
                      --scanners vuln \
                      --severity HIGH,CRITICAL \
                      --format json \
                      --output trivy-image-report.json \
                      --exit-code 0 \
                      "$image"
                '''

                archiveArtifacts artifacts: 'trivy-image-report.json',
                                 fingerprint: true

                sh '''
                    set -eu

                    image="pulsecheck:${BUILD_NUMBER}"

                    echo "===== Critical Vulnerability Quality Gate ====="

                    trivy image \
                      --scanners vuln \
                      --severity CRITICAL \
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

                    echo "Initializing persistent volume permissions..."

                    docker run --rm \
                      --user 0:0 \
                      --entrypoint sh \
                      --mount "source=$volume,target=/app/data" \
                      "pulsecheck:${BUILD_NUMBER}" \
                      -c 'chown app:app /app/data && chmod 750 /app/data'

                    echo "Starting hardened PulseCheck container..."

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

                    for attempt in $(seq 1 30); do
                        if curl -fsS "http://127.0.0.1:${host_port}/health"; then
                            echo

                            docker exec "$container" sh -c \
                              'test -w /app/data && test -s /app/data/pulsecheck.db'

                            echo "PASS: SQLite database exists and is writable on persistent volume"
                            echo "Hardened container smoke test successful"
                            exit 0
                        fi

                        sleep 2
                    done

                    docker logs "$container"

                    echo "Container health test failed"
                    exit 1
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

                        ssh \
                          -i "$EC2_SSH_KEY" \
                          -o BatchMode=yes \
                          -o IdentitiesOnly=yes \
                          -o StrictHostKeyChecking=yes \
                          -o UserKnownHostsFile="$EC2_KNOWN_HOSTS" \
                          -o ConnectTimeout=15 \
                          "$EC2_SSH_USER@$EC2_HOST" \
                          "bash -s -- '${BUILD_NUMBER}'" <<'REMOTE_SCRIPT'

set -Eeuo pipefail

BUILD_TAG="$1"
AWS_REGION="ap-south-1"

APP_CONTAINER="pulsecheck-app"
PREVIOUS_CONTAINER="pulsecheck-app-previous"
CANDIDATE_CONTAINER="pulsecheck-candidate-${BUILD_TAG}"

PRODUCTION_VOLUME="pulsecheck-data"
CANDIDATE_VOLUME="pulsecheck-candidate-${BUILD_TAG}-data"
CANDIDATE_PORT=18081

DEPLOYMENT_COMPLETE=0
OLD_CONTAINER_EXISTS=0
PRODUCTION_SWITCH_STARTED=0
DOCKER_CONFIG_DIR=""

cleanup_ecr_credentials() {
    if [ -n "${DOCKER_CONFIG_DIR:-}" ]; then
        sudo rm -rf "$DOCKER_CONFIG_DIR" || true
        DOCKER_CONFIG_DIR=""
    fi
}

trap cleanup_ecr_credentials EXIT

if sudo docker inspect "$APP_CONTAINER" >/dev/null 2>&1; then
    OLD_CONTAINER_EXISTS=1
fi

ACCOUNT_ID=$(aws sts get-caller-identity \
  --region "$AWS_REGION" \
  --query Account \
  --output text)

REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
IMAGE="${REGISTRY}/pulsecheck:${BUILD_TAG}"

echo "===== AUTHENTICATING WITH AMAZON ECR ====="

DOCKER_CONFIG_DIR=$(sudo mktemp -d /tmp/pulsecheck-ecr.XXXXXX)

aws ecr get-login-password --region "$AWS_REGION" \
  | sudo env DOCKER_CONFIG="$DOCKER_CONFIG_DIR" \
      docker login \
      --username AWS \
      --password-stdin "$REGISTRY"

echo "===== PULLING IMAGE ====="

sudo env DOCKER_CONFIG="$DOCKER_CONFIG_DIR" docker pull "$IMAGE"

sudo rm -rf "$DOCKER_CONFIG_DIR"
DOCKER_CONFIG_DIR=""

echo "===== PREPARING ISOLATED CANDIDATE VOLUME ====="

sudo docker volume rm "$CANDIDATE_VOLUME" >/dev/null 2>&1 || true
sudo docker volume create "$CANDIDATE_VOLUME" >/dev/null

sudo docker run --rm \
  --user 0:0 \
  --entrypoint sh \
  --mount "type=volume,source=$CANDIDATE_VOLUME,target=/app/data" \
  "$IMAGE" \
  -c 'chown app:app /app/data && chmod 750 /app/data'

wait_for_app() {
    local port="$1"

    for attempt in $(seq 1 30); do
        if curl -fsS "http://127.0.0.1:${port}/health" >/dev/null \
          && curl -fsS "http://127.0.0.1:${port}/api/targets" >/dev/null; then
            return 0
        fi

        sleep 2
    done

    return 1
}

rollback_and_cleanup() {
    local exit_status=$?
    trap - EXIT

    cleanup_ecr_credentials

    if [ "$DEPLOYMENT_COMPLETE" -ne 1 ]; then
        echo "===== DEPLOYMENT FAILED: RECOVERY ====="

        if [ "$PRODUCTION_SWITCH_STARTED" -eq 1 ] || \
           [ "$OLD_CONTAINER_EXISTS" -eq 0 ]; then

            if sudo docker inspect "$PREVIOUS_CONTAINER" >/dev/null 2>&1; then
                echo "Previous container found. Restoring it..."

                sudo docker rm -f "$APP_CONTAINER" >/dev/null 2>&1 || true
                sudo docker rename "$PREVIOUS_CONTAINER" "$APP_CONTAINER"
                sudo docker start "$APP_CONTAINER" >/dev/null

                echo "Checking previous application recovery..."

                RECOVERY_OK=0
                for attempt in $(seq 1 30); do
                    if curl -fsS http://127.0.0.1:8000/health >/dev/null; then
                        RECOVERY_OK=1
                        break
                    fi
                    sleep 2
                done

                if [ "$RECOVERY_OK" -eq 1 ]; then
                    echo "Previous application restored successfully."
                else
                    echo "ERROR: Previous application did not recover."
                    sudo docker logs "$APP_CONTAINER" || true
                fi

            elif [ "$OLD_CONTAINER_EXISTS" -eq 1 ]; then
                sudo docker start "$APP_CONTAINER" >/dev/null 2>&1 || true

            else
                sudo docker rm -f "$APP_CONTAINER" >/dev/null 2>&1 || true
            fi

        else
            echo "Failure occurred before production switch."
            echo "Leaving current production and previous rollback containers untouched."
        fi
    fi

    sudo docker stop -t 15 "$CANDIDATE_CONTAINER" >/dev/null 2>&1 || true
    sudo docker rm "$CANDIDATE_CONTAINER" >/dev/null 2>&1 || true
    sudo docker volume rm "$CANDIDATE_VOLUME" >/dev/null 2>&1 || true

    exit "$exit_status"
}

trap rollback_and_cleanup EXIT

echo "===== TESTING NEW IMAGE BEFORE REPLACING PRODUCTION ====="

sudo docker rm -f "$CANDIDATE_CONTAINER" >/dev/null 2>&1 || true

sudo docker run -d \
  --name "$CANDIDATE_CONTAINER" \
  --read-only \
  --cap-drop=ALL \
  --security-opt=no-new-privileges:true \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \
  --pids-limit=100 \
  --mount "type=volume,source=$CANDIDATE_VOLUME,target=/app/data" \
  -p "127.0.0.1:${CANDIDATE_PORT}:8000" \
  -e APP_VERSION="$BUILD_TAG" \
  -e PULSECHECK_DB_PATH=/app/data/pulsecheck.db \
  "$IMAGE"

if ! wait_for_app "$CANDIDATE_PORT"; then
    echo "Candidate application failed health or API checks."
    sudo docker logs "$CANDIDATE_CONTAINER" || true
    exit 1
fi

sudo docker exec "$CANDIDATE_CONTAINER" sh -c \
  'test -w /app/data && test -s /app/data/pulsecheck.db'

echo "PASS: Candidate application and SQLite database verified."

sudo docker stop -t 15 "$CANDIDATE_CONTAINER" >/dev/null
sudo docker rm "$CANDIDATE_CONTAINER" >/dev/null

echo "===== PREPARING PERSISTENT PRODUCTION VOLUME ====="

if sudo docker volume inspect "$PRODUCTION_VOLUME" >/dev/null 2>&1; then
    echo "Existing production volume found. Preserving its data."
else
    sudo docker volume create "$PRODUCTION_VOLUME" >/dev/null
    echo "New production volume created."
fi

sudo docker run --rm \
  --user 0:0 \
  --entrypoint sh \
  --mount "type=volume,source=$PRODUCTION_VOLUME,target=/app/data" \
  "$IMAGE" \
  -c 'chown app:app /app/data && chmod 750 /app/data'

echo "===== PREPARING PRODUCTION SWITCH ====="

if [ "$OLD_CONTAINER_EXISTS" -eq 1 ]; then
    if sudo docker inspect "$PREVIOUS_CONTAINER" >/dev/null 2>&1; then
        echo "Removing older rollback container; current app is available."
        sudo docker rm -f "$PREVIOUS_CONTAINER" >/dev/null
    fi
else
    if sudo docker inspect "$PREVIOUS_CONTAINER" >/dev/null 2>&1; then
        echo "ERROR: Current app is missing but a rollback container exists."
        echo "Refusing to remove the only available previous container."
        exit 1
    fi
fi

PRODUCTION_SWITCH_STARTED=1

if [ "$OLD_CONTAINER_EXISTS" -eq 1 ]; then
    echo "Stopping current application."
    sudo docker stop -t 20 "$APP_CONTAINER" >/dev/null

    echo "Preserving current application for rollback."
    sudo docker rename "$APP_CONTAINER" "$PREVIOUS_CONTAINER"
fi

echo "===== STARTING NEW PRODUCTION CONTAINER ====="

sudo docker run -d \
  --name "$APP_CONTAINER" \
  --restart unless-stopped \
  --read-only \
  --cap-drop=ALL \
  --security-opt=no-new-privileges:true \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \
  --pids-limit=100 \
  --mount "type=volume,source=$PRODUCTION_VOLUME,target=/app/data" \
  -p 127.0.0.1:8000:8000 \
  -e APP_VERSION="$BUILD_TAG" \
  -e PULSECHECK_DB_PATH=/app/data/pulsecheck.db \
  "$IMAGE"

echo "===== VERIFYING PRODUCTION DEPLOYMENT ====="

if ! wait_for_app 8000; then
    echo "New production application failed health or API checks."
    sudo docker logs "$APP_CONTAINER" || true
    exit 1
fi

sudo docker exec "$APP_CONTAINER" sh -c \
  'test -w /app/data && test -s /app/data/pulsecheck.db'

echo "PASS: Production SQLite database exists and is writable."
echo "PASS: Production application health and API checks passed."

DEPLOYMENT_COMPLETE=1

echo "===== DEPLOYMENT SUCCESSFUL ====="
echo "Deployed version: $BUILD_TAG"
echo "Persistent volume: $PRODUCTION_VOLUME"

if [ "$OLD_CONTAINER_EXISTS" -eq 1 ]; then
    echo "Previous container retained: $PREVIOUS_CONTAINER"
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
