import os
from contextlib import asynccontextmanager
from time import perf_counter
from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
)

from app.monitoring import MonitoringService


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
    service = MonitoringService()
    app.state.monitoring_service = service

    try:
        yield
    finally:
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

    try:
        result = await service.check_target(target_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="Monitoring target not found.",
        ) from exc

    return result


@app.delete("/api/targets/{target_id}")
async def delete_target(
    target_id: str,
    request: Request,
):
    service = request.app.state.monitoring_service

    deleted = service.remove_target(target_id)

    if not deleted:
        raise HTTPException(
            status_code=404,
            detail="Monitoring target not found.",
        )

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
