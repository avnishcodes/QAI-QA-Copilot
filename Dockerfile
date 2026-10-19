# Pinned to exact match of installed python playwright version (1.63.0) on Ubuntu 24.04 (Noble)
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    QAI_DATABASE_PATH=/app/data/qai.db \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install project dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create dedicated persistent directories with appropriate permissions
RUN mkdir -p /app/data /app/evidence /app/backend/generated_reports

# Copy application source code (frontend and backend)
COPY backend /app/backend
COPY frontend /app/frontend

# Declare persistent volume mount points
VOLUME ["/app/data", "/app/evidence", "/app/backend/generated_reports"]

# Internal health check verifying backend responsiveness
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/')" || exit 1

# Launch uvicorn listening on container internal interface
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
