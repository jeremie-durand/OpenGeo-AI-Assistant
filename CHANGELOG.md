# Changelog

All notable changes to OpenGeo AI Assistant will be documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

---

## [0.1.0] — 2026-04-07

Initial open-source release — a fork of [Microsoft Earth Copilot](https://github.com/microsoft/Earth-Copilot) adapted to be Azure-independent with flexible LLM provider support.

### Added

- Provider-agnostic LLM client (`llm_client.py`) supporting OpenAI, Anthropic, and Ollama via environment variables (`LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL`, `LLM_BASE_URL`)
- FastAPI backend with STAC/Planetary Computer integration for satellite data discovery
- React 18 + TypeScript + Leaflet frontend for geospatial visualization
- GEOINT analysis agents: terrain, mobility, comparison, extreme weather, building damage
- Docker Compose deployment (backend on port 8000, frontend on port 3000)
- Optional API key authentication (`ENABLE_AUTH`, `API_KEY`)
- Per-IP rate limiting (`RATE_LIMIT_LLM`, `RATE_LIMIT_SEARCH`)
- CORS configuration via `CORS_ORIGINS` env var
- Pre-commit hooks: ruff, black, isort (Python); Husky (TypeScript)
- `CONTRIBUTING.md`, `SECURITY.md`
- `QUICK_DEPLOY.md` for production deployment guidance
- MIT License with attribution to Microsoft

### Security

- Non-root Docker user (`appuser`) in both backend and frontend containers
- nginx security headers: `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`
- DOMPurify for `dangerouslySetInnerHTML` sanitization in the chat interface
- esbuild `drop: ['console', 'debugger']` strips debug output from production bundles

### Changed (from Microsoft upstream)

- Removed Azure OpenAI and Azure Maps dependencies
- Replaced Azure-specific authentication with generic API key middleware
- Replaced hardcoded OpenAI SDK calls with provider-agnostic `llm_client.py`
- Removed Azure Functions port references; all services on standard Docker ports

[Unreleased]: https://github.com/JeremieDurandUdS/opengeo-ai-assistant/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/JeremieDurandUdS/opengeo-ai-assistant/releases/tag/v0.1.0
