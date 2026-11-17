import asyncio
import os
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timedelta, timezone
from hmac import compare_digest
from time import perf_counter

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from app.auth import (
    SESSION_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    generate_csrf_token,
    generate_session_token,
    hash_password,
    hash_session_token,
    verify_password,
)
from app.auth_middleware import AuthenticationMiddleware
from app.database import Database
from app.monitoring import MonitoringService
from app.notifications import WebhookNotifier
from app.scheduler import run_monitoring_scheduler


APP_NAME = "PulseCheck"
APP_VERSION = os.getenv("APP_VERSION", "1.0.0")


REQUEST_COUNT = Counter(
    "pulsecheck_http_requests_total",
    "Total number of HTTP requests received by PulseCheck",
    ["method", "path", "status_code"],
)

REQUEST_LATENCY = Histogram(
    "pulsecheck_http_request_duration_seconds",
    "HTTP request processing duration in seconds",
    ["method", "path"],
)


MONITORED_TARGET_UP = Gauge(
    "pulsecheck_monitored_target_up",
    "Whether the monitored application is UP (1) or DOWN (0).",
    ["target_id", "target_name"],
)

MONITORED_TARGET_RESPONSE_TIME = Gauge(
    "pulsecheck_monitored_target_response_time_seconds",
    "Latest monitored application response time in seconds.",
    ["target_id", "target_name"],
)

MONITORED_TARGET_FAILURES = Gauge(
    "pulsecheck_monitored_target_consecutive_failures",
    "Current consecutive monitoring failures.",
    ["target_id", "target_name"],
)

MONITORED_TARGET_LAST_CHECK = Gauge(
    "pulsecheck_monitored_target_last_check_timestamp_seconds",
    "Unix timestamp of the latest monitoring check.",
    ["target_id", "target_name"],
)

_TARGET_METRICS = (
    MONITORED_TARGET_UP,
    MONITORED_TARGET_RESPONSE_TIME,
    MONITORED_TARGET_FAILURES,
    MONITORED_TARGET_LAST_CHECK,
)

_EXPOSED_TARGET_LABELS: dict[str, tuple[str, str]] = {}


def sync_monitored_target_metrics(service: MonitoringService) -> None:
    """Publish the latest monitoring state as Prometheus gauges."""

    targets = {
        target.id: target
        for target in service.list_targets()
    }

    # Remove stale metric series for deleted targets.
    for target_id, old_labels in list(
        _EXPOSED_TARGET_LABELS.items()
    ):
        target = targets.get(target_id)

        if target is None or old_labels != (
            target.id,
            target.name,
        ):
            for metric in _TARGET_METRICS:
                metric.remove(*old_labels)

            del _EXPOSED_TARGET_LABELS[target_id]

    # Publish the latest result for each registered target.
    for target in targets.values():
        result = service.get_result(target.id)

        if result is None:
            continue

        labels = (target.id, target.name)
        _EXPOSED_TARGET_LABELS[target.id] = labels

        MONITORED_TARGET_UP.labels(*labels).set(
            1 if result.status == "UP" else 0
        )

        MONITORED_TARGET_RESPONSE_TIME.labels(*labels).set(
            result.latency_ms / 1000.0
        )

        MONITORED_TARGET_FAILURES.labels(*labels).set(
            result.consecutive_failures
        )

        MONITORED_TARGET_LAST_CHECK.labels(*labels).set(
            result.checked_at.timestamp()
        )


class TargetCreate(BaseModel):
    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra="forbid",
    )

    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=2048)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    interval = float(
        os.getenv("PULSECHECK_CHECK_INTERVAL_SECONDS", "10")
    )
    db_path = os.getenv(
        "PULSECHECK_DB_PATH",
        "data/pulsecheck.db",
    )

    if interval <= 0:
        raise ValueError(
            "PULSECHECK_CHECK_INTERVAL_SECONDS must be positive."
        )

    test_mode = (
        os.getenv("PULSECHECK_TEST_MODE", "").lower() == "true"
    )
    test_host = os.getenv("PULSECHECK_TEST_ALLOWED_HOST")
    test_ip = os.getenv("PULSECHECK_TEST_ALLOWED_IP")

    if test_mode:
        if not test_host or not test_ip:
            raise ValueError(
                "Test mode requires PULSECHECK_TEST_ALLOWED_HOST "
                "and PULSECHECK_TEST_ALLOWED_IP."
            )

        allowed_test_destinations = {
            test_host: {test_ip}
        }
    else:
        if test_host or test_ip:
            raise ValueError(
                "Test destination allowlist variables require "
                "PULSECHECK_TEST_MODE=true."
            )

        allowed_test_destinations = {}

    notifier: WebhookNotifier | None = None
    service: MonitoringService | None = None
    scheduler_task = None

    try:
        webhook_url = os.getenv("PULSECHECK_WEBHOOK_URL", "").strip()
        if webhook_url:
            notifier = WebhookNotifier(webhook_url)

        database = await asyncio.to_thread(Database, db_path)
        service = MonitoringService(
            allowed_test_destinations=allowed_test_destinations,
            on_transition=(
                notifier.notify_transition
                if notifier is not None
                else None
            ),
        )

        # Restore registered applications and their latest results.
        stored_targets = await asyncio.to_thread(
            database.list_targets
        )

        for target in stored_targets:
            latest_result = await asyncio.to_thread(
                database.get_latest_result,
                target.id,
            )

            service.restore_target(target, latest_result)

        app.state.database = database
        app.state.monitoring_service = service

        scheduler_task = asyncio.create_task(
            run_monitoring_scheduler(
                service,
                interval_seconds=interval,
                database=database,
            ),
            name="pulsecheck-monitoring-scheduler",
        )

        app.state.monitoring_scheduler_task = scheduler_task

        yield

    finally:
        if scheduler_task is not None:
            scheduler_task.cancel()

            try:
                await scheduler_task
            except asyncio.CancelledError:
                pass

        if service is not None:
            await service.close()

        if notifier is not None:
            await notifier.close()


APP_DIR = Path(__file__).resolve().parent

app = FastAPI(
    title=APP_NAME,
    description="Developer-focused website and API monitoring",
    version=APP_VERSION,
    lifespan=lifespan,
)

app.add_middleware(AuthenticationMiddleware)

app.mount(
    "/static",
    StaticFiles(directory=APP_DIR / "static"),
    name="static",
)


@app.middleware("http")
async def collect_request_metrics(request: Request, call_next):
    if request.url.path == "/metrics":
        return await call_next(request)

    start_time = perf_counter()
    status_code = 500

    try:
        response = await call_next(request)
        status_code = response.status_code
        return response

    finally:
        duration = perf_counter() - start_time

        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")

        REQUEST_COUNT.labels(
            method=request.method,
            path=path,
            status_code=str(status_code),
        ).inc()

        REQUEST_LATENCY.labels(
            method=request.method,
            path=path,
        ).observe(duration)


@app.get("/", include_in_schema=False)
async def home():
    return FileResponse(APP_DIR / "templates" / "index.html")



@app.get("/login", include_in_schema=False)
async def login_page():
    return FileResponse(APP_DIR / "templates" / "login.html")


@app.post("/api/auth/login")
async def login(request: Request):
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid login request.") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid login request.")

    username = payload.get("username")
    password = payload.get("password")

    expected_username = os.getenv("PULSECHECK_ADMIN_USERNAME", "")
    expected_hash = os.getenv("PULSECHECK_ADMIN_PASSWORD_HASH", "")

    if not expected_username or not expected_hash:
        raise HTTPException(
            status_code=503,
            detail="Administrator authentication is not configured.",
        )

    if not isinstance(username, str) or not isinstance(password, str):
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    password_valid = verify_password(password, expected_hash)
    username_valid = compare_digest(username, expected_username)

    if not (username_valid and password_valid):
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    database = request.app.state.database

    old_token = request.cookies.get(SESSION_COOKIE_NAME)
    if old_token:
        database.delete_session(hash_session_token(old_token))

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=SESSION_TTL_SECONDS)

    token = generate_session_token()
    token_hash = hash_session_token(token)
    csrf_token = generate_csrf_token()

    database.delete_expired_sessions(now.isoformat())
    database.create_session(
        token_hash=token_hash,
        username=expected_username,
        csrf_token=csrf_token,
        created_at=now.isoformat(),
        expires_at=expires_at.isoformat(),
    )

    secure_cookie = (
        os.getenv("PULSECHECK_COOKIE_SECURE", "true").strip().lower()
        == "true"
    )

    response = JSONResponse({
        "authenticated": True,
        "username": expected_username,
        "csrf_token": csrf_token,
    })

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=secure_cookie,
        samesite="lax",
        path="/",
    )

    return response


@app.get("/api/auth/session")
async def get_auth_session(request: Request):
    session = request.state.auth_session

    return {
        "authenticated": True,
        "username": session["username"],
        "csrf_token": session["csrf_token"],
    }


@app.post("/api/auth/logout")
async def logout(request: Request):
    session = request.state.auth_session
    database = request.app.state.database

    database.delete_session(session["token_hash"])

    secure_cookie = (
        os.getenv("PULSECHECK_COOKIE_SECURE", "true").strip().lower()
        == "true"
    )

    response = JSONResponse({"authenticated": False})
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=secure_cookie,
        samesite="lax",
    )

    return response


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "application": APP_NAME,
    }


@app.get("/version")
async def version():
    return {
        "application": APP_NAME,
        "version": APP_VERSION,
    }


@app.get("/api/demo")
async def demo():
    return {
        "message": "PulseCheck API is working",
        "result": "success",
    }


@app.post("/api/targets", status_code=201)
async def create_target(
    payload: TargetCreate,
    request: Request,
):
    service = request.app.state.monitoring_service
    database = request.app.state.database

    try:
        target = service.register_target(
            name=payload.name,
            url=payload.url,
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from exc

    try:
        await asyncio.to_thread(
            database.save_target,
            target,
        )

    except sqlite3.Error as exc:
        service.remove_target(target.id)

        raise HTTPException(
            status_code=500,
            detail="Could not save monitoring target.",
        ) from exc

    return target


@app.get("/api/targets")
async def list_targets(request: Request):
    service = request.app.state.monitoring_service

    return {
        "targets": [
            {
                "target": target,
                "last_check": service.get_result(target.id),
            }
            for target in service.list_targets()
        ]
    }


@app.get("/api/targets/{target_id}/history")
async def get_target_history(
    target_id: str,
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
):
    database = request.app.state.database

    target = await asyncio.to_thread(
        database.get_target,
        target_id,
    )

    if target is None:
        raise HTTPException(
            status_code=404,
            detail="Monitoring target not found.",
        )

    history = await asyncio.to_thread(
        database.list_history,
        target_id,
        limit,
    )

    return {
        "target_id": target_id,
        "history": history,
    }


@app.get("/api/targets/{target_id}")
async def get_target(
    target_id: str,
    request: Request,
):
    service = request.app.state.monitoring_service

    target = next(
        (
            item
            for item in service.list_targets()
            if item.id == target_id
        ),
        None,
    )

    if target is None:
        raise HTTPException(
            status_code=404,
            detail="Monitoring target not found.",
        )

    return {
        "target": target,
        "last_check": service.get_result(target_id),
    }


@app.post("/api/targets/{target_id}/check")
async def check_target(
    target_id: str,
    request: Request,
):
    service = request.app.state.monitoring_service
    database = request.app.state.database

    try:
        result = await service.check_target(target_id)

    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="Monitoring target not found.",
        ) from exc

    try:
        await asyncio.to_thread(
            database.save_result,
            result,
        )

    except sqlite3.Error as exc:
        raise HTTPException(
            status_code=500,
            detail="Could not save monitoring result.",
        ) from exc

    return result


@app.delete("/api/targets/{target_id}")
async def delete_target(
    target_id: str,
    request: Request,
):
    service = request.app.state.monitoring_service
    database = request.app.state.database

    if not any(
        target.id == target_id
        for target in service.list_targets()
    ):
        raise HTTPException(
            status_code=404,
            detail="Monitoring target not found.",
        )

    try:
        await asyncio.to_thread(
            database.delete_target,
            target_id,
        )

    except sqlite3.Error as exc:
        raise HTTPException(
            status_code=500,
            detail="Could not delete monitoring target.",
        ) from exc

    service.remove_target(target_id)

    return {
        "deleted": True,
        "target_id": target_id,
    }


@app.get("/metrics", include_in_schema=False)
async def metrics(request: Request):
    service = request.app.state.monitoring_service

    sync_monitored_target_metrics(service)

    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
