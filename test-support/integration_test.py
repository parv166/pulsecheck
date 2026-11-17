import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from http.cookiejar import CookieJar
from urllib.request import (
    HTTPCookieProcessor,
    ProxyHandler,
    Request,
    build_opener,
)

APP = "http://127.0.0.1:8000"
PROMETHEUS = "http://prometheus:9090"
SIMULATOR = "http://health-simulator:8080"

TARGET_NAME = "Jenkins E2E Failure Test"

cookie_jar = CookieJar()
opener = build_opener(
    ProxyHandler({}),
    HTTPCookieProcessor(cookie_jar),
)
csrf_token = None


def request_json(url, method="GET", payload=None):
    body = None if payload is None else json.dumps(payload).encode()

    headers = {"Content-Type": "application/json"}

    if method not in {"GET", "HEAD", "OPTIONS"} and csrf_token:
        headers["X-CSRF-Token"] = csrf_token

    request = Request(
        url,
        data=body,
        method=method,
        headers=headers,
    )

    try:
        with opener.open(request, timeout=5) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}

    except HTTPError as exc:
        raw = exc.read()
        message = raw.decode(errors="replace")
        raise RuntimeError(
            f"HTTP {exc.code} from {url}: {message}"
        ) from exc



def request_status(url):
    with opener.open(url, timeout=5) as response:
        return response.status


def wait_for(description, check, timeout=120):
    deadline = time.monotonic() + timeout
    last_error = None

    while time.monotonic() < deadline:
        try:
            if check():
                print(f"PASS: {description}", flush=True)
                return
        except (OSError, URLError, RuntimeError, ValueError, KeyError) as exc:
            last_error = exc

        time.sleep(2)

    raise RuntimeError(
        f"Timed out waiting for {description}. "
        f"Last error: {last_error}"
    )


def get_target(target_id):
    _, data = request_json(f"{APP}/api/targets")

    for item in data["targets"]:
        if item["target"]["id"] == target_id:
            return item

    raise RuntimeError("Test target disappeared from PulseCheck.")


def target_has_status(target_id, expected):
    item = get_target(target_id)
    result = item.get("last_check")

    return (
        result is not None
        and result["status"] == expected
        and (
            expected != "UP"
            or result["consecutive_failures"] == 0
        )
    )


def prometheus_query(expression):
    url = (
        f"{PROMETHEUS}/api/v1/query?"
        + urlencode({"query": expression})
    )
    _, data = request_json(url)

    if data["status"] != "success":
        raise RuntimeError("Prometheus query was unsuccessful.")

    return data["data"]["result"]


def metric_equals(target_id, expected):
    expression = (
        'pulsecheck_monitored_target_up'
        f'{{target_id="{target_id}"}}'
    )
    result = prometheus_query(expression)

    if not result:
        return False

    return float(result[0]["value"][1]) == expected


def alert_is_firing(target_id):
    _, data = request_json(f"{PROMETHEUS}/api/v1/alerts")

    for alert in data["data"]["alerts"]:
        labels = alert["labels"]

        if (
            labels.get("alertname") == "MonitoredApplicationDown"
            and labels.get("target_id") == target_id
            and alert["state"] == "firing"
        ):
            return True

    return False


def alert_is_resolved(target_id):
    _, data = request_json(f"{PROMETHEUS}/api/v1/alerts")

    return not any(
        alert["labels"].get("alertname") == "MonitoredApplicationDown"
        and alert["labels"].get("target_id") == target_id
        for alert in data["data"]["alerts"]
    )


def set_simulator_status(status):
    request_json(
        f"{SIMULATOR}/control",
        method="POST",
        payload={"status": status},
    )
    print(f"Simulator configured to HTTP {status}", flush=True)


def authenticate():
    global csrf_token

    username = os.environ.get("PULSECHECK_ADMIN_USERNAME")
    password = os.environ.get("PULSECHECK_E2E_ADMIN_PASSWORD")

    if not username or not password:
        raise RuntimeError("CI authentication credentials are missing.")

    _, data = request_json(
        f"{APP}/api/auth/login",
        method="POST",
        payload={"username": username, "password": password},
    )

    if data.get("authenticated") is not True:
        raise RuntimeError("PulseCheck CI login was rejected.")

    csrf_token = data.get("csrf_token")

    if not csrf_token:
        raise RuntimeError("PulseCheck login did not return a CSRF token.")

    print("PASS: E2E test authenticated successfully.", flush=True)


def main():
    target_id = None

    try:
        wait_for(
            "PulseCheck health endpoint",
            lambda: request_json(f"{APP}/health")[1].get("status")
            == "healthy",
        )

        authenticate()

        wait_for(
            "Prometheus readiness",
            lambda: request_status(
                f"{PROMETHEUS}/-/ready"
            ) == 200,
        )

        rules = request_json(
            f"{PROMETHEUS}/api/v1/rules"
        )[1]["data"]["groups"]

        rule_names = {
            rule["name"]
            for group in rules
            for rule in group["rules"]
        }

        if "MonitoredApplicationDown" not in rule_names:
            raise RuntimeError(
                "MonitoredApplicationDown rule is not loaded."
            )

        print("PASS: Prometheus alert rule loaded", flush=True)

        _, created = request_json(
            f"{APP}/api/targets",
            method="POST",
            payload={
                "name": TARGET_NAME,
                "url": f"{SIMULATOR}/health",
            },
        )

        target_id = created["id"]
        print(f"Created test target: {target_id}", flush=True)

        _, initial = request_json(
            f"{APP}/api/targets/{target_id}/check",
            method="POST",
        )

        if initial["status"] != "UP" or initial["status_code"] != 200:
            raise RuntimeError(
                f"Initial health check failed: {initial}"
            )

        wait_for(
            "Prometheus records initial UP state",
            lambda: metric_equals(target_id, 1),
            timeout=60,
        )

        set_simulator_status(503)

        wait_for(
            "PulseCheck detects HTTP 503 and DOWN state",
            lambda: target_has_status(target_id, "DOWN"),
            timeout=60,
        )

        wait_for(
            "Prometheus records DOWN metric",
            lambda: metric_equals(target_id, 0),
            timeout=60,
        )

        wait_for(
            "Prometheus MonitoredApplicationDown alert fires",
            lambda: alert_is_firing(target_id),
            timeout=120,
        )

        set_simulator_status(200)

        wait_for(
            "PulseCheck detects recovery and resets failures",
            lambda: target_has_status(target_id, "UP"),
            timeout=60,
        )

        wait_for(
            "Prometheus records recovered UP state",
            lambda: metric_equals(target_id, 1),
            timeout=60,
        )

        wait_for(
            "Prometheus failure alert resolves",
            lambda: alert_is_resolved(target_id),
            timeout=60,
        )

        print(
            "SUCCESS: Full UP -> DOWN -> UP integration test passed.",
            flush=True,
        )

    finally:
        # Restore simulator health before removing the target.
        try:
            set_simulator_status(200)
        except Exception:
            pass

        if target_id:
            try:
                request_json(
                    f"{APP}/api/targets/{target_id}",
                    method="DELETE",
                )
                print("Test target cleaned up.", flush=True)
            except Exception as exc:
                print(f"WARNING: Target cleanup failed: {exc}", flush=True)


if __name__ == "__main__":
    main()
