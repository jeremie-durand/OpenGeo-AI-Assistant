FROM python:3.13-slim

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

RUN groupadd --system appuser && useradd --system --no-create-home --gid appuser appuser

ENV VECTOR_SEARCH_MODEL_DIR=/opt/models
ENV DUCKDB_EXTENSION_DIR=/opt/duckdb_extensions
ARG MODEL_SNAPSHOTS=/opt/models/models--Qdrant--paraphrase-multilingual-MiniLM-L12-v2-onnx-Q/snapshots
ARG MODEL_REVISION=faf4aa4225822f3bc6376869cb1164e8e3feedd0

RUN set -e; \
    python -c "from fastembed import TextEmbedding; \
TextEmbedding('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2', \
cache_dir='/opt/models')"; \
    if [ ! -d "${MODEL_SNAPSHOTS}/${MODEL_REVISION}" ]; then \
        echo "Embedding model revision is not ${MODEL_REVISION}" >&2; exit 1; \
    fi; \
    python -c "import duckdb; \
duckdb.connect(config={'extension_directory': '/opt/duckdb_extensions'})\
.install_extension('httpfs')"; \
    chown -R appuser:appuser /opt/models

COPY --chown=appuser:appuser licenses/paraphrase-multilingual-MiniLM-L12-v2-LICENSE \
    /opt/models/

COPY container-app/*.py ./
COPY container-app/*.json ./
COPY container-app/agents/ ./agents/
COPY container-app/geoint/ ./geoint/
COPY container-app/services/ ./services/
COPY container-app/routers/ ./routers/

RUN mkdir -p /app/uploads /app/data

RUN chown -R appuser:appuser /app
USER appuser

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "fastapi_app:app", "--host", "0.0.0.0", "--port", "8000"]
