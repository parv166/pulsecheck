# 1. Python base image
FROM python:3.14-slim

# 2. Python environment settings
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 3. Application working directory
WORKDIR /app

# 4. Update packages, harden utilities, create app user, and remove unused tools
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
    && chmod u-s /usr/bin/mount /usr/bin/umount \
    && chmod a-s \
        /usr/bin/newgrp \
        /usr/bin/chage \
        /usr/bin/su \
        /usr/bin/chfn \
        /usr/bin/gpasswd \
        /usr/bin/chsh \
        /usr/bin/passwd \
        /usr/bin/expiry \
        /usr/sbin/unix_chkpwd \
    && groupadd --system app \
    && useradd --system --gid app --create-home app \
    && apt-get purge -y passwd \
    && apt-get autoremove -y --purge \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 6. Copy dependency file
COPY requirements.txt .

# 7. Install dependencies
# Install application dependencies, then remove build-time pip
RUN python -m pip install --no-cache-dir --upgrade pip==26.3 \
    && python -m pip install --no-cache-dir -r requirements.txt \
    && python -m pip uninstall -y pip

# 8. Copy application code
COPY --chown=app:app app/ ./app/

# Create persistent database directory
RUN mkdir -p /app/data && chown app:app /app/data

# Run as non-root user
USER app

# 10. Application port
EXPOSE 8000

# 11. Container health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]

# 12. Start application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
