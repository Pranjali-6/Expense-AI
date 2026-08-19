# =============================================================================
# API image — FastAPI. Deliberately slim: no OCR / PDF toolchain lives here,
# because the API never extracts. Extraction is the worker's job.
# =============================================================================
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app:/app/backend

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Test tooling is a build arg so the production image stays free of it while
# the development image can run the suite in place.
ARG INSTALL_TEST=false

COPY backend/requirements/ /tmp/requirements/
RUN pip install --no-cache-dir -r /tmp/requirements/api.txt \
    && if [ "$INSTALL_TEST" = "true" ]; then \
         pip install --no-cache-dir -r /tmp/requirements/test.txt; \
       fi \
    && rm -rf /tmp/requirements

COPY backend/ /app/backend/
COPY parsers/ /app/parsers/
COPY workers/ /app/workers/

# Run as an unprivileged user.
RUN groupadd --system --gid 1001 expense \
    && useradd --system --uid 1001 --gid expense --no-create-home expense \
    && chown -R expense:expense /app
USER expense

EXPOSE 8000

CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--app-dir", "/app/backend", \
     "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--reload", "--reload-dir", "/app/backend"]
