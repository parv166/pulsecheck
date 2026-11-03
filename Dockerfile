# 1. Python base image
FROM python:3.14-slim

# 2. Python environment settings
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 3. Application working directory
WORKDIR /app

# 4. Create a non-root application user
RUN groupadd --system app && \
    useradd --system --gid app --create-home app

# 5. Copy dependency file
COPY requirements.txt .

# 6. Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# 7. Copy application code
COPY --chown=app:app app/ ./app/

# 8. Run as non-root user
USER app

# 9. Application port
EXPOSE 8000

# 10. Container health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]

# 11. Start application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
