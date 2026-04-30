# Contributing to OpenGeo AI Assistant

Thank you for your interest in contributing! This document covers how to set up your environment, submit changes, and follow project conventions.

---

## Table of Contents

- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
- [Project Structure](#project-structure)
- [Making Changes](#making-changes)
- [Code Style](#code-style)
- [Testing](#testing)
- [Submitting a Pull Request](#submitting-a-pull-request)
- [Reporting Bugs](#reporting-bugs)
- [Requesting Features](#requesting-features)

---

## Getting Started

1. **Fork** the repository on GitHub
2. **Clone** your fork locally:

   ```bash
   git clone https://github.com/your-username/opengeo-ai-assistant.git
   cd opengeo-ai-assistant
   ```

3. Add the upstream remote:

   ```bash
   git remote add upstream https://github.com/original-owner/opengeo-ai-assistant.git
   ```

---

## Development Setup

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose
- An LLM API key (OpenAI or Anthropic)

### Git hooks setup

The project uses **Husky + lint-staged** (frontend) and **pre-commit** (backend) for code quality checks on commit. Husky v9 owns the git hook — do **not** run `pre-commit install`, as it would be bypassed anyway.

**Full-stack (recommended):**

```bash
cd opengeo-ai-assistant/web-ui && npm install
# Husky installs the git hook automatically via the `prepare` script.
# The hook calls pre-commit (Python) first, then lint-staged (frontend).

pip install pre-commit
# No need to run `pre-commit install` — Husky handles the hook.
```

**Frontend only:**

```bash
cd opengeo-ai-assistant/web-ui && npm install
# Lint-staged (Prettier) runs on commit. Python hooks are skipped if pre-commit is not installed.
```

**Backend only:**

```bash
pip install pre-commit
# Run manually: pre-commit run --all-files
# Or install as a standalone hook (only if you are NOT running npm install in web-ui):
pre-commit install
```

### Run with Docker

```bash
cp .env.example .env
# Fill in your LLM_PROVIDER, LLM_API_KEY, and LLM_MODEL in .env

docker compose up --build
```

Frontend is available at `http://localhost:3000`.
Backend is available at `http://localhost:8000`.

### Run services separately

Each service can be built and started independently using its Docker Compose service name.

**Backend only:**

```bash
docker compose up --build backend
# API available at http://localhost:8000
```

**Frontend only** (automatically starts the backend too, via `depends_on`):

```bash
docker compose up --build frontend
# UI available at http://localhost:3000
```

**Rebuild a single service after code changes:**

```bash
docker compose up --build backend   # or frontend
```

**View logs for one service:**

```bash
docker compose logs -f backend   # or frontend
```

---

## Project Structure

```
opengeo-ai-assistant/
├── container-app/     # Python FastAPI backend
│   ├── fastapi_app.py         # Main app and all routes
│   ├── llm_client.py          # Provider-agnostic LLM client (use this for all LLM calls)
│   ├── semantic_translator.py # Query → STAC search pipeline
│   └── geoint/                # GEOINT agents (terrain, mobility, weather, etc.)
├── web-ui/            # React 18 + TypeScript + Vite frontend
│   ├── Dockerfile             # Frontend image (nginx serving React build)
│   └── src/
│       ├── components/        # UI components
│       ├── services/          # API client and data services
│       └── ui/                # Layout and panel components
└── Dockerfile         # Backend image (Python FastAPI)
```

---

## Making Changes

### Branching

- Branch off `develop`, not `main`
- Use a descriptive branch name: `feat/my-feature`, `fix/bug-description`

```bash
git checkout develop
git pull upstream develop
git checkout -b feat/my-feature
```

### Commit messages

Use the following prefixes:

| Prefix | When to use |
| ------ | ----------- |
| `feat:` | New feature |
| `fix:` | Bug fix |
| `docs:` | Documentation only |
| `refactor:` | Code change with no behavior change |
| `test:` | Adding or updating tests |
| `chore:` | Build, config, dependency updates |

Example: `feat: add cloud cover filter to STAC query`

### Key rules

- **Always use `llm_client.py`** for LLM calls — never import `openai` or `anthropic` SDKs directly
- **New GEOINT agents** must follow the pattern in `geoint/` (router + agent + tools file)
- **Type hints required** on all Python functions
- **Line length:** 88 chars max (Python), enforced by Black
- **No bare `except:`** — catch specific exception types

---

## Code Style

### Python

Enforced automatically on commit via `pre-commit` (ruff → isort → black).

- **Formatter:** [Black](https://github.com/ambv/black) — `black .`
- **Linter:** [Ruff](https://github.com/astral-sh/ruff-pre-commit) — `ruff check --fix .`
- **Import sort:** [isort](https://github.com/pycqa/isort) with `--profile black` — `isort .`
- Naming: `snake_case` functions/variables, `PascalCase` classes, `UPPER_SNAKE_CASE` constants
- Line length: 88 chars (Black default)
- Type hints required on all functions

Run all checks manually:

```bash
pre-commit run --all-files
```

### TypeScript

Enforced automatically on commit via Husky + lint-staged (Prettier on staged files).

- **Formatter:** [Prettier](https://prettier.io/) — config in `web-ui/.prettierrc`
- Single quotes, 2-space indent, trailing commas (`es5`), 100 char line width
- Use `const` by default; `let` only when reassignment is needed
- Prefer named exports over default exports

Run manually:

```bash
cd opengeo-ai-assistant/web-ui
npx prettier --write .
```

---

## Testing

### Backend

Run pytest inside the backend container from the repo root:

```bash
docker compose run --rm -u root \
  -v "$(pwd)/opengeo-ai-assistant/container-app/tests:/app/tests:ro" \
  backend \
  sh -c "pip install --no-cache-dir pytest pytest-asyncio && pytest tests/ -v --tb=short"
```

### Frontend

```bash
cd opengeo-ai-assistant/web-ui && npm run test:run
```

---

## Submitting a Pull Request

1. Push your branch and open a PR **against `develop`**
2. Fill in the PR description — what changed and why
3. Ensure the app builds cleanly with `docker compose up --build`
4. Check `http://localhost:8000/api/health` returns `200`
5. Request a review — a maintainer will respond within a few days

PRs to `main` are reserved for releases and are handled by maintainers.

---

## Reporting Bugs

Open a [GitHub Issue](../../issues/new) and include:

- What you did
- What you expected to happen
- What actually happened
- Your OS, Docker version, and LLM provider
- Relevant logs from `docker compose logs`

---

## Requesting Features

Open a [GitHub Issue](../../issues/new) with the `enhancement` label. Describe the use case, not just the solution — this helps maintainers understand the need before discussing implementation.

---

## Security Issues

Please **do not** open a public issue for security vulnerabilities. See [SECURITY.md](SECURITY.md) for the responsible disclosure process.
