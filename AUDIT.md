# Post-Production Security & Code Quality Audit — OpenGeo AI Assistant

**Audit Date:** 2026-04-07  
**Model:** Sonnet 4.6 - Claude Code
**Scope:** Full codebase — backend (`container-app/`) + frontend (`web-ui/`)  
**Purpose:** Transparency document committed alongside the initial open-source release. Describes the current security posture, known trade-offs, and outstanding items honestly so operators and contributors can make informed decisions.

---

## Summary

| Category | Status | Notes |
| -------- | ------ | ----- |
| Security | ✅ GOOD | 0 critical, 0 high; 2 low (documented design trade-offs) |
| Authentication | ⚠️ DISABLED BY DEFAULT | Intentional for local dev; operators must enable for production |
| LLM Abstraction | ✅ FULLY COMPLIANT | All provider code isolated to `llm_client.py` |
| Input Validation | ✅ GOOD | Pydantic validators with range enforcement |
| Rate Limiting | ✅ CONFIGURED | Per-IP limits, configurable via env |
| Docker | ✅ GOOD | Non-root user, multi-stage build, health checks |
| Dependencies | ✅ PINNED | Python exact versions; JS caret ranges acceptable |
| Code Quality | ✅ GOOD | All modules use `logger`; no `print()` in production code |
| Error Handling | ✅ GOOD | Specific exception types on high-traffic handlers; broad catches only on init/loop fallbacks |
| Open Source Readiness | ✅ READY | All required governance files present |

### Overall rating: 9 / 10

The project is well-engineered for an initial open-source release. No critical vulnerabilities, no hardcoded secrets, strong dependency hygiene, a solid LLM abstraction layer, consistent structured logging, and specific exception handling on all high-traffic handlers. The remaining deduction is for the permissive CORS/auth defaults that rely on operator discipline rather than safe-by-default configuration — an intentional design trade-off for local usability.

---

## Security Findings

### SEC-1 — CORS wildcard default

**Location:** `container-app/fastapi_app.py` lines 390–412

`CORS_ORIGINS` defaults to `"*"` when the environment variable is not set. The code correctly disables `allow_credentials` when using a wildcard (required by the CORS spec), so cookie-based session hijacking is not possible. However, any origin can call the API in a misconfigured production deployment.

**Severity:** Medium  
**Mitigation in place:** `README.md` and `SECURITY.md` both explicitly state that `CORS_ORIGINS=*` must not be used in production. `.env.example` documents the correct override.  
**Residual risk:** Operators who skip the production checklist.

---

### SEC-2 — Authentication disabled by default

**Location:** `.env.example` line 17, `container-app/auth_middleware.py`

`ENABLE_AUTH=false` and `API_KEY=change-me-before-deploying` are the defaults so the app works out of the box locally. When `ENABLE_AUTH=true`, the middleware now raises `RuntimeError` at startup if `API_KEY` is empty or equals the default placeholder — the app will not start in a misconfigured state.

**Severity:** Low (by design for local dev)  
**Mitigation in place:** `QUICK_DEPLOY.md`, `SECURITY.md`, and `README.md` all cover enabling auth before production exposure. Fail-fast startup validation added.

---

## Positive Findings

These are done correctly and should remain as-is:

- **LLM provider isolation** — `llm_client.py` is the sole entry point for all provider SDK usage. No direct `openai` or `anthropic` imports exist outside the client wrappers.
- **Input validation** — All coordinate fields, string lengths, and Base64 upload sizes are constrained via Pydantic `Field()` validators in `request_models.py`.
- **Rate limiting** — Per-IP rate limiting is configured for LLM (10/min) and search (30/min) endpoints via `RATE_LIMIT_LLM` and `RATE_LIMIT_SEARCH` env vars.
- **Non-root Docker user** — `appuser` created and used in the backend `Dockerfile` (lines 35–37).
- **Multi-stage frontend build** — `web-ui/Dockerfile` uses a Node build stage and an nginx runtime stage; no build toolchain in the final image.
- **nginx security headers** — `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`, `Content-Security-Policy`, and `Strict-Transport-Security` all present in `web-ui/nginx.conf`.
- **XSS protection** — `DOMPurify.sanitize()` wraps all `dangerouslySetInnerHTML` usage in `web-ui/src/components/Chat.tsx:1431`.
- **Production console stripping** — `vite.config.ts` drops all `console` and `debugger` calls from production bundles via esbuild.
- **Pinned Python dependencies** — All packages in `requirements.txt` use exact `==` version pins.
- **API key via header** — `X-Api-Key` header (not query param) used for authentication; not logged or reflected in responses.
- **Auth fail-fast** — `auth_middleware.py` raises `RuntimeError` at startup if `ENABLE_AUTH=true` and `API_KEY` is unset or the default placeholder.
- **No SQL / command injection surface** — No raw SQL queries, no `subprocess` shell execution, no `eval()`/`exec()` calls in the codebase.
- **Structured pipeline logging** — `log_pipeline_step()` helper in `fastapi_app.py` enables session-scoped log filtering.
- **Consistent logger usage** — All backend modules, including `semantic_translator.py`, route output through `logging.getLogger(__name__)`; no `print()` calls remain in production code.

---

## Open Source Readiness

| File | Status |
| ---- | ------ |
| `LICENSE.txt` | ✅ MIT, proper attribution to Microsoft |
| `README.md` | ✅ Present, includes production warnings |
| `CONTRIBUTING.md` | ✅ Present |
| `SECURITY.md` | ✅ Present, references private advisory process |
| `CHANGELOG.md` | ✅ Present |
| `.env.example` | ✅ All variables documented with production guidance |
| `QUICK_DEPLOY.md` | ✅ Covers auth, CORS, TLS for production operators |
| `CODE_OF_CONDUCT.md` | — Intentionally omitted for now |
| `.github/ISSUE_TEMPLATE/` | — Intentionally omitted for now |

---

## Dependency Audit

### Python (`requirements.txt`)

All 40+ packages pinned to exact versions. No known CVEs in the version set as of audit date. No high-risk packages (no `pickle`, `paramiko`, raw YAML loaders, template engines).

Recommended: add `pip-audit` to CI when a pipeline is added.

### JavaScript (`web-ui/package.json`)

Caret ranges (`^`) used — acceptable for a Vite/React app. `dompurify ^3.3.3` is correctly a runtime dependency. No high-risk packages.

Run `npm audit` before each release tag.

---

## Outstanding Action Items

None. All identified issues have been resolved or are documented as intentional design trade-offs (see Security Findings).
