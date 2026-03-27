# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**open Geospatial Copilot** is an open-source, AI-powered geospatial analysis and satellite data visualization platform — a fork of Microsoft's Earth Copilot adapted to be Azure-independent with flexible LLM provider support.

## Commands

### Running services

```bash
# Build and run complete app (frontend + backend on port 8000)

docker compose up --build
```

```bash
# Health check

curl http://localhost:8000/api/health
```

### Testing

None for now

## Architecture

```
open-geospatial-copilot/
├── web-ui/            # React 18 + TypeScript + Vite frontend
├── container-app/     # Python FastAPI backend (main app logic lives here)
├── mcp-server/        # Optional Model Context Protocol server
└── Dockerfile         # Multi-stage: builds React dist, then embeds in Python image
docker-compose.yml     # Single service on port 8000
.env / .env.example    # LLM provider, API keys, feature flags
```

### Frontend (`web-ui/src/`)

- Entry: `main.tsx` → `App.tsx`
- Key components: `MapView` (Leaflet), `ChatPanel`, `CatalogPanel`, `GeointOverlay`, `ModelSelector`
- State: React hooks + TanStack Query for server state
- API client: `services/api.ts` proxied to `localhost:8000` in dev (`vite.config.ts`)

### Backend (`container-app/`)

**Request Flow:**

1. `fastapi_app.py` — Main FastAPI app, all routes defined here (large file ~323KB)
2. `POST /api/query` → `semantic_translator.py` / `generic_query_translator.py` → `location_resolver.py` → `collection_name_mapper.py` → tile selection/rendering
3. GEOINT requests (`/api/geoint/*`) → `geoint/router_agent.py` → specialized agent

**LLM Abstraction:**

- `llm_client.py` — Provider-agnostic LLM client; reads `LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL` from env
- `anthropic_client.py` — Anthropic-specific wrapper
- All agents must use `llm_client.py`, never import provider SDKs directly

**GEOINT Agents (`geoint/`):**

- `router_agent.py` — Orchestrates and routes to the right agent
- `terrain_agent.py`, `mobility_agent.py`, `comparison_agent.py`, `extreme_weather_agent.py`, `building_damage_agent.py`
- Each agent has a paired `*_tools.py` with the underlying geospatial tool implementations

**Data Pipeline:**

- `collection_profiles.py` + `veda_collection_profiles.py` — STAC collection metadata
- `hybrid_rendering_system.py` + `tile_selector.py` — Map tile rendering
- `raster_data_fetcher.py` — COG/raster data retrieval via Planetary Computer / STAC APIs
- `pc_rendering_config.json` — 122KB rendering profile config (do not edit manually)

## Configuration

- Environment variables are defined in `.env`

```bash
LLM_PROVIDER=openai|anthropic
LLM_API_KEY=<key>
LLM_MODEL=<model-name>          # e.g. gpt-4-1106-preview, claude-3-opus-20240229
LLM_BASE_URL=<optional>         # Custom endpoint
WEATHER_DATA_SOURCE=planetary_computer|open_meteo
ENABLE_AUTH=true|false
CORS_ORIGINS=<comma-separated>
```

## Claude guidelines

- Understand the pipeline before modifying code

- Keep changes minimal and scoped

- Reuse existing utilities and patterns

- Always use `llm_client.py` for LLM calls. Never add direct `openai` or `anthropic` SDK imports outside the client wrappers.

- Map rendering uses LEaflet

- New agents must follow existing patterns in `geoint/`

- Follow request flow: /api/query → semantic translator → location resolver → tile selector

- Satellite data discovery uses standard STAC API endpoints (Planetary Computer, VEDA, or local).

- Agent orchestration uses `semantic_kernel` 1.36.2. Follow existing agent patterns in `geoint/` when adding new agents.

### Python: Key Patterns to Follow

- Type hints required for all code

- Functions must be focused and small

- Line length: 88 chars maximum

- PEP 8 naming (snake_case for functions/variables)

- Class names in PascalCase

- Constants in UPPER_SNAKE_CASE

- Document with docstrings, avoid comments

- Use f-strings for formatting

### Typescript: Key Patterns to Follow

None for now

### Testing guidelines

None for now

### Branching strategy

- `develop` — integration branch (PRs target here)

- `main` — production

Commit prefixes: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`

## CI

None for now
