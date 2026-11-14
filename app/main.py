import asyncio
import os
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import perf_counter

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
)

from app.database import Database
from app.monitoring import MonitoringService
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

    database = await asyncio.to_thread(Database, db_path)
    service = MonitoringService()
    scheduler_task = None

    try:
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

        await service.close()


app = FastAPI(
    title=APP_NAME,
    description="DevOps and Infrastructure Operations Platform",
    version=APP_VERSION,
    lifespan=lifespan,
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


@app.get("/")
async def home():
    return {
        "application": APP_NAME,
        "message": "Welcome to PulseCheck",
        "version": APP_VERSION,
    }


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
async def metrics():
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
