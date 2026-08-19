# =============================================================================
# Worker image — carries the full deterministic extraction toolchain.
#
#   tesseract-ocr   OCR fallback for scanned statements
#   poppler-utils   pdf2image rasterisation
#   ghostscript     Camelot ruled-table extraction
#   libgl / libglib OpenCV runtime (deskew)
#
# This image is intentionally heavier than the API image; it is the only place
# that ever touches a PDF's bytes.
# =============================================================================
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app:/app/backend \
    OMP_NUM_THREADS=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        libpq5 \
        tesseract-ocr \
        tesseract-ocr-eng \
        poppler-utils \
        ghostscript \
        libgl1 \
        libglib2.0-0 \
        qpdf \
    && rm -rf /var/lib/apt/lists/*

# Test tooling is a build arg so the production image stays free of it while
# the development image can run the suite in place.
ARG INSTALL_TEST=false

COPY backend/requirements/ /tmp/requirements/
RUN pip install --no-cache-dir -r /tmp/requirements/worker.txt \
    && if [ "$INSTALL_TEST" = "true" ]; then \
         pip install --no-cache-dir -r /tmp/requirements/test.txt; \
       fi \
    && rm -rf /tmp/requirements

COPY backend/ /app/backend/
COPY parsers/ /app/parsers/
COPY workers/ /app/workers/
COPY tools/ /app/tools/

RUN groupadd --system --gid 1001 expense \
    && useradd --system --uid 1001 --gid expense --no-create-home expense \
    && chown -R expense:expense /app
USER expense

CMD ["celery", "-A", "workers.celery_app", "worker", "--loglevel=INFO"]
