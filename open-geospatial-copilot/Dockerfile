# Multi-stage Dockerfile for Complete Open Geospatial Copilot Application
# Stage 1: Build React Frontend
# Stage 2: Python Backend with Static Files

# =============================================================================
# STAGE 1: Build React Frontend
# =============================================================================
FROM node:20-alpine AS frontend-build

WORKDIR /frontend

# Copy package files for dependency installation
COPY ../web-ui/package*.json ./

# Install Node.js dependencies
RUN npm ci

# Copy frontend source code
COPY ../web-ui/ ./web-ui/

# Fix permissions and build the React application for production
WORKDIR /frontend/web-ui
RUN chmod +x ../node_modules/.bin/* && npm run build
WORKDIR /frontend

# List contents of dist directory for debugging
RUN ls -la web-ui/dist/

# =============================================================================
# STAGE 2: Python Backend with Static Files
# =============================================================================
FROM python:3.12-slim

# Install system dependencies for geospatial processing
# GDAL is required for rasterio to read COG files (terrain analysis)
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

# Set GDAL environment variables for rasterio
ENV GDAL_CONFIG=/usr/bin/gdal-config
ENV CPLUS_INCLUDE_PATH=/usr/include/gdal
ENV C_INCLUDE_PATH=/usr/include/gdal

# Set working directory
WORKDIR /app

# Copy Python requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
  pip install --no-cache-dir -r requirements.txt

# Copy all Python modules
COPY container-app/*.py ./

# Copy JSON config files (pc_rendering_config.json, etc.)
COPY container-app/*.json ./

# Copy agents folder (Vision Agent and other AI agents)
COPY container-app/agents/ ./agents/

# Copy geoint folder (GEOINT agents and modules)
COPY container-app/geoint/ ./geoint/

# Create static directory and copy built React app
RUN mkdir -p /app/static
COPY --from=frontend-build /frontend/web-ui/dist/ /app/static/

# Create uploads directory (if needed)
RUN mkdir -p /app/uploads

# Set environment variables
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Expose port 8000
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:8000/api/health || exit 1

# Start the FastAPI application on port 8000
CMD ["uvicorn", "fastapi_app:app", "--host", "0.0.0.0", "--port", "8000"]