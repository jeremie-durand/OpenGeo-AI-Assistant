# Quick Deploy

### 1. Prerequisites

- Docker
- API key for your LLM provider (OpenAI or Anthropic/Claude supported)

### 2. Environment Setup

Copy `.env.example` to `.env` in the repo root and fill in your values:

```sh
cp .env.example .env
# Edit .env with your LLM provider, key, and model
```

### 3. Start the app

```sh
docker compose up --build
```

- Frontend will be available at [http://localhost:8000](http://localhost:8000)

### 4. Health Check

```sh
curl http://localhost:3000/api/health
```

### 5. Test a Prompt (Smoke Test)

```sh
curl -X POST http://localhost:3000/api/query \
 -H 'Content-Type: application/json' \
 -d '{"query": "Show me satellite imagery of Paris"}'
```

---

# Production Deployment

### 1. Generate an API Key

```sh
openssl rand -hex 32
```

Copy the output — this is your `API_KEY`.

### 2. Enable Auth in `.env`

```sh
ENABLE_AUTH=true
API_KEY=<paste-key-here>
```

Also set your production URLs:

```sh
FRONTEND_URL=https://your-domain.com
# CORS_ORIGINS is derived from FRONTEND_URL automatically
```

### 3. Build and Start

```sh
docker compose up --build
```

### 4. Test Auth is Working

```sh
# Should return 401 (unauthorized)
curl -s -o /dev/null -w "%{http_code}" http://localhost:3000/api/query \
  -X POST -H 'Content-Type: application/json' -d '{}'

# Should return 200 (ok)
curl -s -o /dev/null -w "%{http_code}" http://localhost:3000/api/query \
  -X POST \
  -H 'Content-Type: application/json' \
  -H 'X-Api-Key: <your-api-key>' \
  -d '{"query": "Show me satellite imagery of Paris"}'
```

Health check and the root `/` are always open (no key required).
