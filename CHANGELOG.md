# Changelog

All notable changes to OpenGeo AI Assistant will be documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added

- `chat.welcome` translation key (EN/FR) for the chat greeting, which was the last hardcoded English string in the chat UI.
- `isWelcome` flag on `ChatMessage`, marking the mount-time greeting so it can be re-translated in place.
- `src/i18n/__tests__/translations.test.tsx` covering catalog key parity between languages and the host language contract (`sdss-lang` + `sdss-lang-change`, including the cross-tab `storage` path).
- CI now builds the production bundle (`npm run build`) in the frontend job. Vitest never invokes the production bundler, so a Rolldown-incompatible `vite.config.ts` shipped in two releases with every test green.

### Changed

- The chat greeting is rendered through `t()` and re-translates when the host switches language after mount, instead of being fixed to English at mount time.
- Production minifier is now oxc (Rolldown's own). `minify: 'esbuild'` remains valid in Vite 8 but requires the `esbuild` package, which Vite 8 no longer installs.

### Fixed

- `vite build` failed outright with `TypeError: manualChunks is not a function`: Vite 8 bundles with Rolldown, which accepts only the function form of `manualChunks`, not Rollup's object/record form. The production build had been broken since the Vite 8 upgrade.
- `src/utils/tileLayerFactory.ts` did not compile — a bad paste had left its module header comment unterminated, swallowing both imports and the `TileLayerOptions` interface. The same paste had also dropped the `else` branch of the TileJSON check, which is restored: the per-collection config supplies the zoom range, and the chosen range is logged. The zoom numbers the lost code hardcoded are deliberately *not* reinstated — `getCollectionConfig()` already returns the same 6-22 range for optical collections and for its default fallback, while HLS (8+) and MODIS (8-10+) need higher floors that the old override would have lowered, reintroducing tile 404s.
- Type errors across the web UI: `@types/node` was not in `tsconfig.json` `types` (so `global` and `NodeJS` were unresolved), an optional-chained `.length` was compared without a default, a `QueryCategory` entry was missing its `icon`, a STAC item callback required a `bbox` its input does not always carry, and a leftover `atlas.Map` cast referenced Azure Maps types removed in 0.1.0-alpha.

### Removed

- `esbuild: { drop: ['console', 'debugger'] }` — the option needs the esbuild transform, and oxc exposes no equivalent, so it was dead configuration. **Console output is no longer stripped from production bundles**; restoring that would mean adopting terser.

---

## [0.2.0-alpha] — 2026-09-02

### Added

- `_llm_text()` accepts an optional `response_format`, forwarded to the provider only when set, so callers can opt into API-level JSON enforcement (`{"type": "json_object"}`). Small local models ignore prompt-level "return only JSON" instructions.
- Canadian entries in `_KNOWN_BBOXES` (Canada, Québec, Ontario, and the main Québec cities), with accented and unaccented keys so both spellings resolve in the keyword fallback.
- `_anchor_bbox()` replaces an LLM-supplied bbox with the vetted one whenever the reported `location_name` is a known location.
- `tests/test_generic_query_translator.py` covering the above.

### Changed

- `build_stac_query_agent()` now requests JSON mode and anchors the returned bbox, instead of trusting the model's coordinates verbatim.

---

## [0.1.3-alpha] — 2026-08-28

Tagged `v.0.1.3-alpha` — the stray dot after `v` is a typo in the tag name, and it breaks `v*.*.*` matching and version sorting. Kept as-is so the link below resolves.

### Added

- FR/EN bilingual support in the web UI (`i18n/I18nContext.tsx`, `i18n/translations.ts`). Language is shared with a host platform through the `sdss-lang` localStorage key and the `sdss-lang-change` event, so an embedding site's language toggle drives the chat UI.

---

## [0.1.2-alpha] — 2026-07-13

No release notes were kept for this version. See the compare link below.

---

## [0.1.1-alpha] — 2026-04-30

No release notes were kept for this version. See the compare link below.

---

## [0.1.0-alpha] — 2026-04-08

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
- Pre-commit hooks: ruff, black, isort (Python); Husky, prettier (TypeScript)
- `CONTRIBUTING.md`, `SECURITY.md`
- `QUICK_DEPLOY.md` for production deployment guidance
- MIT License with attribution to Microsoft
- GitHub Actions CI workflow (`test-dependencies.yml`) running backend and frontend smoke tests on every PR
- Backend pytest suite: per-package import checks (20 packages), Pydantic model validation, LLMClient configuration, FastAPI `/api/health` smoke test
- `requirements-test.txt` pinning `pytest` and `pytest-asyncio` separately from production deps
- Frontend Vitest smoke tests (`TerrainWorkflow.test.tsx`) covering event flow and message formatting
- Runtime environment variable injection for frontend — config changes no longer require a container rebuild

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

[Unreleased]: https://github.com/jeremie-durand/OpenGeo-AI-Assistant/compare/v0.2.0-alpha...HEAD
[0.2.0-alpha]: https://github.com/jeremie-durand/OpenGeo-AI-Assistant/compare/v.0.1.3-alpha...v0.2.0-alpha
[0.1.3-alpha]: https://github.com/jeremie-durand/OpenGeo-AI-Assistant/compare/v0.1.2-alpha...v.0.1.3-alpha
[0.1.2-alpha]: https://github.com/jeremie-durand/OpenGeo-AI-Assistant/compare/v0.1.1-alpha...v0.1.2-alpha
[0.1.1-alpha]: https://github.com/jeremie-durand/OpenGeo-AI-Assistant/compare/v0.1.0-alpha...v0.1.1-alpha
[0.1.0-alpha]: https://github.com/jeremie-durand/OpenGeo-AI-Assistant/releases/tag/v0.1.0-alpha
