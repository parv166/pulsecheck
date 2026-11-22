# PulseCheck

Self-hosted application-availability and infrastructure monitoring built with FastAPI, SQLite, Prometheus, Grafana, Alertmanager, node-exporter, and cAdvisor. An optional systemd collector reads EC2 and CloudWatch metrics.

## Features

- Scheduled HTTP/HTTPS checks with UP/DOWN state, latency, failure counts, and last-success timestamps.
- SQLite persistence for targets, monitoring history, incidents, and hashed session tokens.
- Incident history and recovery tracking.
- Optional Discord notifications on DOWN and recovery transitions.
- Prometheus metrics and alert rules; Grafana dashboards; Alertmanager email routing.
- Optional EC2 instance status and CloudWatch CPU/network snapshot.
- Consistent SQLite backups, AES-GCM encrypted backups, restore verification, rollback and volume-migration regression tests.
- Hardened Docker runtime and Jenkins CI with signed DHI VEX verification and a VEX-aware Trivy gate.

## Requirements

- Docker Engine and Docker Compose v2
- Python 3.14 for local development/tests
- DHI registry access and Docker Buildx for the hardened image build

## Local setup

1. Copy `.env.example` to `.env` and set a strong unique `GRAFANA_ADMIN_PASSWORD`.
2. Install development dependencies:

   ```bash
   python3.14 -m venv .venv
   source .venv/bin/activate
   python -m pip install -r requirements-dev.txt
   ```

3. Generate administrator credentials:

   ```bash
   python scripts/create-admin-hash.py
   ```

   The helper prompts for a username and password, stores a scrypt hash in `secrets/auth.local.env`, and sets restrictive permissions. Passwords must be at least 14 characters. The file is ignored by Git and loaded by the PulseCheck Compose service.

4. Optionally add `PULSECHECK_WEBHOOK_URL=https://discord.com/api/webhooks/...` to `secrets/auth.local.env`. Treat it as a secret.

5. For the full stack, create the Gmail app-password file at `/etc/pulsecheck-secrets/gmail_app_password`, as referenced by the Alertmanager configuration. Review the sender and recipient in `alertmanager/alertmanager.yml` for your environment.

6. Start the stack:

   ```bash
   docker compose up -d --build
   docker compose ps
   ```

   To start only PulseCheck: `docker compose up -d --build pulsecheck`.

## Local URLs

All published service ports bind to loopback by default.

| Service | URL |
| --- | --- |
| PulseCheck | http://127.0.0.1:8000/login |
| Prometheus | http://127.0.0.1:9090 |
| Grafana | http://127.0.0.1:3000 |
| Alertmanager | http://127.0.0.1:9093 |

Grafana username is `admin`; password is `GRAFANA_ADMIN_PASSWORD` from `.env`.

The credential helper sets `PULSECHECK_COOKIE_SECURE=false` for local HTTP. Do not expose this configuration directly to the internet. For HTTPS deployments set `PULSECHECK_COOKIE_SECURE=true` in `secrets/auth.local.env` and configure TLS at the reverse proxy/load balancer.

Useful commands:

```bash
docker compose logs -f pulsecheck
docker compose logs -f prometheus grafana alertmanager
docker compose restart pulsecheck
docker compose down
```

`docker compose down` preserves named volumes. Avoid `docker compose down -v` unless intentionally deleting application and monitoring data.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `APP_VERSION` | `1.0.0` | `/version` value |
| `PULSECHECK_DB_PATH` | `data/pulsecheck.db`; Compose uses `/app/data/pulsecheck.db` | SQLite path |
| `PULSECHECK_CHECK_INTERVAL_SECONDS` | `10` | Scheduler interval |
| `PULSECHECK_ADMIN_USERNAME` | unset | Administrator username |
| `PULSECHECK_ADMIN_PASSWORD_HASH` | unset | scrypt password hash |
| `PULSECHECK_COOKIE_SECURE` | `true` | Secure session-cookie flag |
| `PULSECHECK_WEBHOOK_URL` | unset | Optional Discord webhook |
| `PULSECHECK_METRICS_FILE` | `/run/pulsecheck-metrics/metrics.json` | EC2 snapshot path |
| `AWS_REGION` | `ap-south-1` in collector | AWS region |
| `PULSECHECK_EC2_INSTANCE_ID` | unset | EC2 instance ID |

Do not commit `.env`, `secrets/auth.local.env`, webhook URLs, email app passwords, AWS credentials, or backup passphrases.

## Authentication and safety

- Login is at `/login`. Admin credentials must be configured before login works.
- Passwords use scrypt. Random session tokens are stored in SQLite as hashes and expire after 8 hours.
- Session cookies are HttpOnly and SameSite=Lax. Secure is controlled by `PULSECHECK_COOKIE_SECURE`.
- State-changing authenticated requests require the session CSRF token in `X-CSRF-Token`.
- Health, version, metrics, login, and static assets are public; dashboard/API routes require authentication.
- Target URLs must use HTTP/HTTPS. Credentials in URLs, local/internal hostnames, private/non-public IPs, and DNS resolutions to non-public addresses are rejected. Private test destinations require explicit test mode.

## HTTP endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Public health check |
| `GET /version` | Public application version |
| `GET /metrics` | Public Prometheus metrics |
| `GET /login` | Public login page |
| `POST /api/auth/login` | Create session |
| `GET /api/auth/session` | Current session and CSRF token |
| `POST /api/auth/logout` | Revoke session; CSRF required |
| `GET /api/targets` | List targets/latest results |
| `POST /api/targets` | Create target; CSRF required |
| `GET /api/targets/{id}` | Target details |
| `POST /api/targets/{id}/check` | Manual check; CSRF required |
| `GET /api/targets/{id}/history` | Check history |
| `DELETE /api/targets/{id}` | Delete target; CSRF required |
| `GET /api/incidents` | List/filter incidents |
| `GET /api/incidents/summary` | Incident summary |
| `GET /api/servers` | EC2 snapshot, if configured |

## Monitoring and AWS metrics

Prometheus scrapes `pulsecheck:8000`, `node-exporter:9100`, and `cadvisor:8080`. Included rules cover PulseCheck/exporter availability, monitored application availability, host CPU, memory, and root-disk thresholds. Prometheus routes alerts through Alertmanager; Grafana is provisioned with the Prometheus datasource.

The optional EC2 collector is separate from the application container. Its systemd service/timer is under `deploy/systemd/`; it reads EC2 status and CloudWatch CPU/network metrics and atomically writes a JSON snapshot. The timer runs every 60 seconds. Snapshots older than 180 seconds are marked stale. Configure the AWS region, instance ID, AWS CLI, snapshot path, and least-privilege read-only IAM permissions. Never bake AWS credentials into the image.

## Development and tests

```bash
python -m pytest -v -p no:cacheprovider

BUILD_NUMBER=local PULSECHECK_IMAGE=pulsecheck:local bash scripts/run-e2e-ci.sh
bash scripts/test-deploy-rollback.sh
PULSECHECK_IMAGE=pulsecheck:local bash scripts/test-deploy-rollback-real.sh
PULSECHECK_IMAGE=pulsecheck:local bash scripts/test-sqlite-volume-migration.sh
```

Standard image:

```bash
docker build -t pulsecheck:local .
```

DHI image (requires registry access):

```bash
docker login dhi.io
docker buildx build --pull --platform linux/amd64 --load \
  -f Dockerfile.dhi -t pulsecheck:dhi-local .
```

## CI/CD and production safety

Jenkins runs unit tests, builds the DHI image, retrieves and verifies the signed DHI VEX attestation for the pinned runtime digest, generates raw and VEX-adjusted Trivy reports, and blocks on unsuppressed HIGH/CRITICAL findings. It then runs hardened-container smoke, monitoring integration, rollback regression, and SQLite migration tests.

Production actions are opt-in. `DEPLOY_TO_AWS` defaults to `false`; EC2 preflight, database backup, and database restore are separate operator-selected modes. Keep production parameters disabled during ordinary CI validation.

The DHI runtime refresh build passed 109 unit tests plus the VEX-aware Trivy gate, hardened smoke test, UP/DOWN/UP integration test, rollback tests, and SQLite migration test. That CI run did not push to ECR or deploy to EC2.

## SQLite backups

`scripts/backup_sqlite_readonly.py` creates a consistent SQLite online backup without modifying its source and can inspect integrity, required tables, row counts, size, and SHA-256.

`scripts/backup_crypto.py` encrypts backups using AES-256-GCM with a scrypt-derived key. The passphrase must contain at least 32 UTF-8 bytes. Supply it via `PULSECHECK_BACKUP_PASSPHRASE`, not a command-line argument, and store it separately from the backup.

```bash
python scripts/backup_sqlite_readonly.py \
  --source data/pulsecheck.db \
  --destination /secure/backups/pulsecheck.sqlite

read -rsp "Backup passphrase: " PULSECHECK_BACKUP_PASSPHRASE
echo
export PULSECHECK_BACKUP_PASSPHRASE

python scripts/backup_crypto.py --encrypt \
  --source /secure/backups/pulsecheck.sqlite \
  --destination /secure/backups/pulsecheck.sqlite.pcbak

unset PULSECHECK_BACKUP_PASSPHRASE
```

Use a unique destination for each backup; the scripts refuse to overwrite an existing file. Test decryption and SQLite integrity before relying on a backup for recovery.
