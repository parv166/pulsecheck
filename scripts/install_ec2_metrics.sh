#!/usr/bin/env bash
set -Eeuo pipefail

BUILD_TAG="${1:?Usage: install_ec2_metrics.sh BUILD_NUMBER ASSET_DIR}"
ASSET_DIR="${2:?Usage: install_ec2_metrics.sh BUILD_NUMBER ASSET_DIR}"
ASSET_PATTERN="^/tmp/pulsecheck-metrics-${BUILD_TAG}\.[A-Za-z0-9]+$"

if [[ ! "$ASSET_DIR" =~ $ASSET_PATTERN ]]; then
    echo "ERROR: Asset directory does not match this deployment."
    exit 1
fi

if [[ ! -d "$ASSET_DIR" || -L "$ASSET_DIR" ]]; then
    echo "ERROR: Asset directory is missing or unsafe."
    exit 1
fi

COLLECTOR_SOURCE="$ASSET_DIR/collect_ec2_metrics.py"
SERVICE_SOURCE="$ASSET_DIR/pulsecheck-metrics.service"
TIMER_SOURCE="$ASSET_DIR/pulsecheck-metrics.timer"
VALIDATOR_SOURCE="$ASSET_DIR/validate_metrics_snapshot.py"

for file in "$COLLECTOR_SOURCE" "$SERVICE_SOURCE" "$TIMER_SOURCE" "$VALIDATOR_SOURCE"; do
    if [ ! -s "$file" ]; then
        echo "ERROR: Required deployment file is missing: $file"
        exit 1
    fi
done

AWS_BIN="$(command -v aws || true)"

if [ -z "$AWS_BIN" ]; then
    echo "ERROR: AWS CLI is not installed on the EC2 host."
    exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
    echo "ERROR: curl is required for the IMDSv2 identity check."
    exit 1
fi

# Read instance identity through IMDSv2 without printing the token.
TOKEN="$(curl -fsS --max-time 3 \
    -X PUT \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
    http://169.254.169.254/latest/api/token)"

INSTANCE_ID="$(curl -fsS --max-time 3 \
    -H "X-aws-ec2-metadata-token: $TOKEN" \
    http://169.254.169.254/latest/meta-data/instance-id)"

unset TOKEN

if [[ ! "$INSTANCE_ID" =~ ^i-[0-9a-f]+$ ]]; then
    echo "ERROR: Could not validate EC2 instance identity."
    exit 1
fi

AWS_REGION="ap-south-1"

echo "Preparing the dedicated metrics service account..."

if ! getent group pulsecheck-metrics >/dev/null; then
    sudo groupadd --system pulsecheck-metrics
fi

if ! id pulsecheck-metrics >/dev/null 2>&1; then
    sudo useradd \
        --system \
        --gid pulsecheck-metrics \
        --home-dir /nonexistent \
        --shell /usr/sbin/nologin \
        --no-create-home \
        pulsecheck-metrics
fi

sudo usermod --home /nonexistent --shell /usr/sbin/nologin pulsecheck-metrics

sudo install -d \
    -o pulsecheck-metrics \
    -g pulsecheck-metrics \
    -m 0755 \
    /var/lib/pulsecheck-metrics

echo "Installing the collector and systemd units..."

sudo install -D \
    -o root -g root -m 0644 \
    "$COLLECTOR_SOURCE" \
    /usr/local/libexec/pulsecheck/collect_ec2_metrics.py

sudo install \
    -o root -g root -m 0644 \
    "$VALIDATOR_SOURCE" \
    /usr/local/libexec/pulsecheck/validate_metrics_snapshot.py

sudo install \
    -o root -g root -m 0644 \
    "$SERVICE_SOURCE" \
    /etc/systemd/system/pulsecheck-metrics.service

sudo install \
    -o root -g root -m 0644 \
    "$TIMER_SOURCE" \
    /etc/systemd/system/pulsecheck-metrics.timer

echo "Writing non-secret collector configuration..."

sudo tee /etc/pulsecheck-metrics.conf >/dev/null <<EOF
AWS_REGION=$AWS_REGION
PULSECHECK_EC2_INSTANCE_ID=$INSTANCE_ID
PULSECHECK_METRICS_FILE=/var/lib/pulsecheck-metrics/metrics.json
AWS_PAGER=
AWS_CLI_HISTORY=disabled
AWS_EC2_METADATA_V1_DISABLED=true
AWS_CONFIG_FILE=/dev/null
AWS_SHARED_CREDENTIALS_FILE=/dev/null
HOME=/tmp
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
EOF

sudo chown root:root /etc/pulsecheck-metrics.conf
sudo chmod 0644 /etc/pulsecheck-metrics.conf

echo "Checking IAM role access as the collector account..."

if ! sudo -u pulsecheck-metrics env \
    HOME=/tmp \
    AWS_REGION="$AWS_REGION" \
    AWS_DEFAULT_REGION="$AWS_REGION" \
    AWS_PAGER= \
    AWS_EC2_METADATA_V1_DISABLED=true \
    AWS_CONFIG_FILE=/dev/null \
    AWS_SHARED_CREDENTIALS_FILE=/dev/null \
    "$AWS_BIN" sts get-caller-identity >/dev/null 2>&1; then

    echo "ERROR: The collector account cannot access the EC2 instance role."
    exit 1
fi

echo "PASS: Collector account can access the instance role."

echo "Validating and enabling systemd units..."

sudo systemd-analyze verify \
    /etc/systemd/system/pulsecheck-metrics.service \
    /etc/systemd/system/pulsecheck-metrics.timer

sudo systemctl daemon-reload

echo "Running the first collection before enabling the timer..."

if ! sudo systemctl start pulsecheck-metrics.service; then
    sudo journalctl -u pulsecheck-metrics.service -n 30 --no-pager || true
    echo "ERROR: Initial metrics collection failed."
    exit 1
fi

if ! sudo test -s /var/lib/pulsecheck-metrics/metrics.json; then
    echo "ERROR: Collector did not produce a metrics snapshot."
    sudo journalctl -u pulsecheck-metrics.service -n 30 --no-pager || true
    exit 1
fi

sudo /usr/bin/python3 \
    /usr/local/libexec/pulsecheck/validate_metrics_snapshot.py \
    /var/lib/pulsecheck-metrics/metrics.json \
    "$INSTANCE_ID"


sudo systemctl enable --now pulsecheck-metrics.timer
sudo systemctl is-active --quiet pulsecheck-metrics.timer

echo "PASS: PulseCheck metrics collector installed and timer active."
