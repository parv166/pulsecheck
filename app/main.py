import os

from fastapi import FastAPI


APP_NAME = "PulseCheck"
APP_VERSION = os.getenv("APP_VERSION", "1.0.0")

app = FastAPI(
    title=APP_NAME,
    description="DevOps and Infrastructure Operations Platform",
    version=APP_VERSION
)


@app.get("/")
async def home():
    return {
        "application": APP_NAME,
        "message": "Welcome to PulseCheck",
        "version": APP_VERSION
    }


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "application": APP_NAME
    }


@app.get("/version")
async def version():
    return {
        "application": APP_NAME,
        "version": APP_VERSION
    }


@app.get("/api/demo")
async def demo():
    return {
        "message": "PulseCheck API is working",
        "result": "success"
    }
