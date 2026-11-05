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
                    docker build \
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
                      -p "127.0.0.1:${host_port}:8000" \
                      -e APP_VERSION="ci-${BUILD_NUMBER}" \
                      "pulsecheck:${BUILD_NUMBER}"

                    for attempt in $(seq 1 30); do
                        if curl -fsS \
                          "http://127.0.0.1:${host_port}/health"; then
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
    }

    post {
        success {
            echo 'PulseCheck CI pipeline completed successfully!'
        }

        failure {
            echo 'Pipeline failed. Check the stage logs.'
        }
    }
}
