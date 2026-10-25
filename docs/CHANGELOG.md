# Changelog

One entry per completed phase (plus notable fixes). Newest first.

## Phase 0 - Skeleton (2026-10-25)

* Compose stack: Caddy (auto-HTTPS edge), web (Vite/React static build), api
  (FastAPI), Redis, MediaMTX (RTMP 1935 / SRT 8890, HTTP auth hook wired to the API).
  All images multi-arch; CI builds them for linux/arm64.
* `core/`: typed settings (`OMNI_*`), Fernet encryption at rest with key rotation,
  SQLite/Postgres engine helper (WAL), SQLModel tables for the core data model,
  Redis key names, structlog logging.
* `api/`: `/healthz` (liveness), `/api/v1/health` (db/redis/mediamtx/relay),
  single-admin login with argon2id + signed HttpOnly cookie, login rate limiting,
  SPA static serving, `omnistream` CLI (`secret`, `hash-password`, `check-config`).
* `scripts/init_env.py`: generates `.env` with fresh secrets (stdlib only).
* Tooling: `Makefile` (`up/down/logs/test/lint/...`), ruff + mypy `--strict`,
  pytest with fakeredis, GitHub Actions CI.
* Docs: README (fresh VM in <10 commands), ARCHITECTURE, platform notes, owner TODOs,
  OCI/firewall notes.
