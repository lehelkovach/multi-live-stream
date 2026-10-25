# OmniStream

Self-hosted multistream relay + unified chat, built for an OCI Ampere A1 (arm64) VM.

* **Relay:** OBS sends one stream to this server; it fans out to YouTube, Twitch,
  Facebook, Kick, X or any custom RTMP target. Stream copy by default, no re-encode.
* **Unified chat:** every destination's chat in one mobile-first dashboard; reply to
  one platform or broadcast to all.
* **Mobile apps (optional):** Android-in-a-container (Redroid) for apps without RTMP ingest.

Everything runs with Docker Compose. See `docs/ARCHITECTURE.md` for the design and
`docs/CHANGELOG.md` for what is done so far.

## Fresh OCI Ampere VM in under 10 commands

Ubuntu 22.04/24.04 arm64 image, ports 80/443 (web), 1935/tcp (RTMP) and 8890/udp (SRT)
open in the OCI security list **and** the VM firewall (see `deploy/README.md`).

```bash
# 1. Docker (official convenience script; includes the compose plugin)
curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker "$USER" && newgrp docker

# 2. Code
git clone https://github.com/lehelkovach/multi-live-stream.git omnistream && cd omnistream

# 3. Secrets + config (prints the admin password and OBS stream key ONCE)
python3 scripts/init_env.py --domain stream.example.com   # or leave --domain out for localhost

# 4. Bring everything up
docker compose up -d --build        # (= make up)

# 5. Check
curl -k https://stream.example.com/healthz
docker compose ps
```

Open `https://<domain>/`, log in with the printed admin password, and point OBS at
`rtmp://<domain>:1935/live` with the printed stream key.

If you set `OMNI_DOMAIN` to a real DNS name Caddy obtains a Let's Encrypt
certificate automatically. With `localhost` or a bare IP it issues a self-signed
certificate (expect a browser warning / use `curl -k`).

## What works today (Phase 3)

* Ingest with a secret stream key (RTMP + SRT), rejected otherwise.
* Relay: one supervised `ffmpeg -c copy` per enabled destination, auto-restart with
  backoff, per-destination status/bitrate/last error, global start/stop.
* Mobile-first dashboard at `https://<domain>/`: login, add/edit/toggle destinations
  (platform presets for YouTube/Twitch/Facebook/Kick/X), live status over a
  WebSocket, OBS settings with copy buttons. API docs at `/api/docs`.
* Unified chat (read): connect Twitch, YouTube, Kick and Facebook accounts via OAuth
  (or a pasted token); messages from every platform land in one pane with platform
  tags and badges, and are kept as history. Register the OAuth apps first: see
  `docs/TODO-owner.md`.
* Not yet: replies/broadcast (Phase 4), mobile apps (Phase 5), metrics/backups (Phase 6).

## Day-to-day

| Command              | What it does                                              |
|----------------------|-----------------------------------------------------------|
| `make up`            | build images and start the stack (detached)               |
| `make down`          | stop the stack (data volumes are kept)                    |
| `make logs S=api`    | follow logs (all services when `S` is omitted)            |
| `make ps`            | service status + health                                   |
| `make test`          | Python unit tests (`make test-integration` needs binaries)|
| `make test-e2e`      | Playwright dashboard smoke test (redis-server + Chromium)|
| `make lint`          | ruff + mypy (strict) + TypeScript type-check              |
| `make secret`        | print a random key for `.env`                             |
| `make hash-password` | print an argon2 hash for `OMNI_ADMIN_PASSWORD_HASH`       |
| `make check-config`  | validate `.env` inside the api container                  |

## Development (outside Docker)

Requires Python 3.12 + [uv](https://docs.astral.sh/uv/), Node 22, and for the
integration tests `ffmpeg`, `redis-server` and a `mediamtx` binary on `PATH`
(`MEDIAMTX_BIN=/path/to/mediamtx` also works).

```bash
make venv                 # uv sync
make test && make lint
make dev-api              # uvicorn with reload on :8000 (reads .env)
make dev-web              # vite dev server on :5173, proxies /api to :8000
```

## Layout

```
ingest/   MediaMTX config (RTMP :1935, SRT :8890, auth hook -> API)
core/     shared settings, crypto (Fernet), DB (SQLModel), Redis, logging
api/      FastAPI app: auth, destinations, chat, websockets, ingest hooks
relay/    ffmpeg supervisor (one isolated process per destination)
chat/     platform chat adapters
web/      React + TypeScript dashboard (mobile-first)
mobile/   Redroid + Appium automation (optional `mobile` profile)
deploy/   Caddyfile, OCI / firewall notes
docs/     ARCHITECTURE, CHANGELOG, platform notes, owner TODOs
tests/    unit + integration tests
```

## Security model (short version)

* Dashboard + API behind a single admin login (argon2id hash, signed HttpOnly cookie).
* Publishers must know the secret stream key; MediaMTX asks the API before accepting a publish.
* Stream keys and OAuth tokens are encrypted at rest with Fernet (`OMNI_SECRET_KEY`).
* No secrets in git: `.env` is ignored, `.env.example` documents every variable.
* Internal hooks (`/internal/*`) are blocked at the Caddy edge.
