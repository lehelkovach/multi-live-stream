# OmniStream - operator + developer entry points.
# `make` with no target prints this help.

.DEFAULT_GOAL := help
SHELL := /bin/bash

COMPOSE ?= docker compose
UV      ?= uv
NPM     ?= npm --prefix web
S       ?=            # service name for logs/restart, e.g. make logs S=api

.PHONY: help init-env up down restart build ps logs venv test test-integration test-e2e lint fmt \
        web-install web-build web-lint secret hash-password check-config compose-config \
        dev-api dev-web clean backup prune

help: ## show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- stack ----
init-env: ## create .env with generated secrets (prints admin password once)
	python3 scripts/init_env.py

up: ## build images and start the whole stack
	$(COMPOSE) up -d --build

down: ## stop the stack (keeps volumes)
	$(COMPOSE) down

restart: ## restart one service (S=api) or all
	$(COMPOSE) restart $(S)

build: ## build images without starting
	$(COMPOSE) build

ps: ## service status
	$(COMPOSE) ps

logs: ## follow logs (S=api to filter)
	$(COMPOSE) logs -f --tail=200 $(S)

compose-config: ## validate docker-compose.yml
	$(COMPOSE) config -q && echo "compose config ok"

check-config: ## validate .env inside the api image
	$(COMPOSE) run --rm --no-deps -T api omnistream check-config

# ------------------------------------------------------------ dev / test ----
venv: ## install Python deps into .venv (uv)
	$(UV) sync --frozen

test: venv ## unit tests
	$(UV) run pytest -q -m "not integration"

test-integration: venv ## integration tests (need mediamtx, ffmpeg, redis-server)
	$(UV) run pytest -q -m integration

test-e2e: venv web-build ## Playwright dashboard smoke test (needs redis-server + chromium)
	cd web && npx playwright test

lint: venv web/node_modules/.package-lock.json ## ruff + mypy + tsc
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy
	$(NPM) run typecheck

fmt: venv ## auto-format + autofix
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

web/node_modules/.package-lock.json: web/package.json web/package-lock.json
	$(NPM) ci

web-install: web/node_modules/.package-lock.json ## npm ci for the dashboard

web-build: web-install ## production build of the dashboard into web/dist
	$(NPM) run build

web-lint: web-install ## TypeScript type-check only
	$(NPM) run typecheck

dev-api: venv ## run the API locally with reload (reads .env)
	$(UV) run uvicorn api.main:create_app --factory --reload --host 127.0.0.1 --port 8000

dev-web: web-install ## run the Vite dev server (proxies /api to :8000)
	$(NPM) run dev

# ---------------------------------------------------------------- secrets ----
secret: ## print a random key (Fernet-compatible) for .env
	@python3 -c "import base64,secrets;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"

hash-password: ## print an argon2 hash for OMNI_ADMIN_PASSWORD_HASH
	@if command -v $(UV) >/dev/null 2>&1; then $(UV) run --frozen omnistream hash-password; \
	else $(COMPOSE) run --rm --no-deps api omnistream hash-password; fi

# ---------------------------------------------------------------- backups ----
backup: ## snapshot the database inside the api container and copy it to ./backups/
	$(COMPOSE) exec -T api omnistream backup
	mkdir -p backups && $(COMPOSE) cp api:/data/backups/. ./backups/
	@echo "copied to ./backups/ - keep a copy of .env with it (encryption key)"

prune: ## delete chat history older than OMNI_CHAT_RETENTION_DAYS (default 30)
	$(COMPOSE) exec -T api omnistream prune

clean: ## remove caches and build outputs
	rm -rf .pytest_cache .mypy_cache .ruff_cache web/dist
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
