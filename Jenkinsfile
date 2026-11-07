
pipeline {
    agent any

    options {
        timestamps()
        disableConcurrentBuilds()
        skipDefaultCheckout(true)
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
                      --skip-files "**/pip/_vendor/bom.cdx.json" \
                      --format table \
                      --exit-code 0 \
                      "$image"

                    echo "===== Saving JSON Security Report ====="

                    trivy image \
                      --scanners vuln \
                      --severity HIGH,CRITICAL \
                      --skip-files "**/pip/_vendor/bom.cdx.json" \
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
                      --skip-files "**/pip/_vendor/bom.cdx.json" \
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
                    host_port=$((18000 + BUILD_NUMBER))

                    cleanup() {
                        docker rm -f "$container" >/dev/null 2>&1 || true
                    }

                    trap cleanup EXIT

                    docker run -d \
                      --name "$container" \
                      --security-opt=no-new-privileges:true \
                      -p "127.0.0.1:${host_port}:8000" \
                      -e APP_VERSION="ci-${BUILD_NUMBER}" \
                      "pulsecheck:${BUILD_NUMBER}"

                    for attempt in $(seq 1 30); do
                        if curl -fsS "http://127.0.0.1:${host_port}/health"; then
                            echo
                            echo "Container smoke test successful"
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

        stage('Push Image to Amazon ECR') {
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

set -euo pipefail

BUILD_TAG="$1"
AWS_REGION="ap-south-1"

ACCOUNT_ID=$(aws sts get-caller-identity \
  --region "$AWS_REGION" \
  --query Account \
  --output text)

REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
IMAGE="${REGISTRY}/pulsecheck:${BUILD_TAG}"

echo "Authenticating with ECR using EC2 IAM role..."

DOCKER_CONFIG_DIR=$(sudo mktemp -d /tmp/pulsecheck-ecr.XXXXXX)

cleanup() {
    sudo rm -rf "$DOCKER_CONFIG_DIR"
}

trap cleanup EXIT

aws ecr get-login-password --region "$AWS_REGION" \
  | sudo env DOCKER_CONFIG="$DOCKER_CONFIG_DIR" \
      docker login \
      --username AWS \
      --password-stdin "$REGISTRY"

echo "Pulling image: $IMAGE"

sudo env DOCKER_CONFIG="$DOCKER_CONFIG_DIR" \
  docker pull "$IMAGE"

cleanup
trap - EXIT

OLD_IMAGE=""

if sudo docker inspect pulsecheck-app >/dev/null 2>&1; then
    OLD_IMAGE=$(sudo docker inspect \
      --format '{{.Config.Image}}' pulsecheck-app)
fi

restore_previous() {
    if [ -z "$OLD_IMAGE" ]; then
        echo "No previous image available for rollback."
        return 1
    fi

    OLD_VERSION="${OLD_IMAGE##*:}"

    echo "Restoring previous image: $OLD_IMAGE"

    sudo docker run -d \
      --name pulsecheck-app \
      --restart unless-stopped \
      --security-opt=no-new-privileges:true \
      -p 127.0.0.1:8000:8000 \
      -e APP_VERSION="$OLD_VERSION" \
      "$OLD_IMAGE"

    for attempt in $(seq 1 20); do
        if curl -fsS http://127.0.0.1:8000/health >/dev/null; then
            echo "Previous version restored successfully."
            return 0
        fi
        sleep 2
    done

    echo "Rollback health check failed."
    sudo docker logs pulsecheck-app || true
    return 1
}

echo "Removing previous container..."

sudo docker rm -f pulsecheck-app >/dev/null 2>&1 || true

echo "Starting new PulseCheck container..."

if ! sudo docker run -d \
    --name pulsecheck-app \
    --restart unless-stopped \
    --security-opt=no-new-privileges:true \
    -p 127.0.0.1:8000:8000 \
    -e APP_VERSION="$BUILD_TAG" \
    "$IMAGE"; then

    echo "New container failed to start."
    restore_previous
    exit 1
fi

HEALTH_OK=0

for attempt in $(seq 1 20); do
    if curl -fsS http://127.0.0.1:8000/health >/dev/null; then
        HEALTH_OK=1
        break
    fi
    sleep 2
done

if [ "$HEALTH_OK" -ne 1 ]; then
    echo "New deployment failed its health check."

    sudo docker logs pulsecheck-app || true

    sudo docker rm -f pulsecheck-app >/dev/null 2>&1 || true

    restore_previous

    exit 1
fi

echo "Deployment successful!"
echo "Deployed version: $BUILD_TAG"

REMOTE_SCRIPT
                }
            }
        }
    }

    post {
        success {
            echo 'PulseCheck CI, ECR publishing and EC2 deployment completed successfully!'
        }

        failure {
            echo 'Pipeline failed. Check the stage logs.'
        }
    }
}
