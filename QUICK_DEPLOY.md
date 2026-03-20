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

### 3. Start the Backend
```sh
docker compose up --build
```

- The backend will be available at [http://localhost:8000](http://localhost:8000)

### 4. Health Check
```sh
curl http://localhost:8000/api/health
```

### 5. Test a Prompt (Smoke Test)
```sh
curl -X POST http://localhost:8000/api/query \
	-H 'Content-Type: application/json' \
	-d '{"query": "Show me satellite imagery of Paris"}'
```

### 6. Web UI (Optional)
- See `web-ui/README.md` for instructions to run the frontend and point it to the backend at `http://localhost:8000`.