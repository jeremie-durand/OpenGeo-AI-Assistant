# Security Policy

## Supported Versions

| Version | Supported |
| ------- | --------- |
| latest (`main`) | ✅ Yes |
| older tags | ❌ No |

Only the latest commit on `main` is actively maintained. If you find a vulnerability, please report it against the current `main` branch.

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Please report security issues via [GitHub private security advisories](https://github.com/JeremieDurandUdS/opengeo-ai-assistant/security/advisories/new).

Include:

- A description of the vulnerability and its potential impact
- Steps to reproduce (proof-of-concept code if applicable)
- The version or commit SHA where the issue was found

## Out of Scope

The following are considered acceptable risks for self-hosted, open-source deployments and are **not** treated as vulnerabilities:

- Rate-limit bypass on a self-hosted instance where the operator controls the network
- Authentication disabled by default (`ENABLE_AUTH=false`) — operators must enable auth before exposing the service to the internet (see `QUICK_DEPLOY.md`)
- CORS wildcard (`CORS_ORIGINS=*`) on a private/development deployment — production deployments must set this to their domain

## Security Best Practices for Operators

Before exposing this service publicly:

1. Set `ENABLE_AUTH=true` and generate a strong `API_KEY` (`openssl rand -hex 32`)
2. Set `CORS_ORIGINS` to your specific frontend domain — never use `*` in production
3. Run behind a reverse proxy (nginx, Caddy) with TLS
4. Review rate-limit settings (`RATE_LIMIT_LLM`, `RATE_LIMIT_SEARCH`) for your threat model

See `QUICK_DEPLOY.md` and `.env.example` for configuration details.
