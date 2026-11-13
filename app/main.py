import os
from time import perf_counter

from fastapi import FastAPI, Request
from fastapi.responses import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
)


APP_NAME = "PulseCheck"
APP_VERSION = os.getenv("APP_VERSION", "1.0.0")


# Total HTTP requests, grouped by method, route and status code.
REQUEST_COUNT = Counter(
    "pulsecheck_http_requests_total",
    "Total number of HTTP requests received by PulseCheck",
    ["method", "path", "status_code"],
)

# Time spent processing HTTP requests, grouped by method and route.
REQUEST_LATENCY = Histogram(
    "pulsecheck_http_request_duration_seconds",
    "HTTP request processing duration in seconds",
    ["method", "path"],
)


app = FastAPI(
    title=APP_NAME,
    description="DevOps and Infrastructure Operations Platform",
    version=APP_VERSION,
)


@app.middleware("http")
async def collect_request_metrics(request: Request, call_next):
    # Do not count Prometheus scraping its own metrics endpoint.
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

        # Use the route template rather than the raw URL.
        # This avoids creating unlimited metric labels.
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


@app.get("/",)
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


@app.get("/metrics", include_in_schema=False)
async def metrics():
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
