FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    libexpat1 \
    libexpat1-dev \
    libgeos-dev \
    libxml2 \
    libxml2-dev \
    gdal-bin \
    libgdal-dev \
    libproj-dev \
    proj-bin \
    curl \
    && rm -rf /var/lib/apt/lists/*

ENV GDAL_CONFIG=/usr/bin/gdal-config
ENV CPLUS_INCLUDE_PATH=/usr/include/gdal
ENV C_INCLUDE_PATH=/usr/include/gdal

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY container-app/*.py ./
COPY container-app/*.json ./
COPY container-app/agents/ ./agents/
COPY container-app/geoint/ ./geoint/

RUN mkdir -p /app/uploads

RUN groupadd --system appuser && useradd --system --no-create-home --gid appuser appuser \
    && chown -R appuser:appuser /app
USER appuser

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "fastapi_app:app", "--host", "0.0.0.0", "--port", "8000"]
