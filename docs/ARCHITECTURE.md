# OmniStream architecture

Status: Phase 0 (skeleton) complete. This document is updated at the start of
each phase with the plan for that phase, and at the end with what was actually
built.

## Big picture

```
                 OBS / phone                         viewers
                     │ RTMP :1935 / SRT :8890             ▲
                     ▼                                    │
┌──────────────── MediaMTX (ingest) ───────────────┐      │
│  auth hook ──► api:/internal/mediamtx/auth       │      │
│  RTSP :8554 (internal)   API :9997 (internal)    │      │
└───────┬──────────────────────────────────────────┘      │
        │ one ffmpeg per destination (-c copy)            │
        ▼                                                 │
┌── relay (python supervisor) ──┐   ┌── YouTube / Twitch / Facebook / Kick / X / custom RTMP
│  reconcile loop, backoff,     │──►│
│  status -> Redis + DB         │   └── (Phase 5) Redroid "virtual camera" apps
└───────────────────────────────┘
        ▲ config (DB)          ▲ status (Redis pub/sub)
        │                      │
┌── api (FastAPI) ─────────────┴───────┐      ┌── chat adapters (Phase 3/4) ──┐
│  auth (argon2 + cookie), REST, WS    │◄────►│  Twitch IRC, YouTube, Kick, FB │
│  serves /api, /ws, /healthz          │      └────────────────────────────────┘
└──────────────┬───────────────────────┘
               │
┌── caddy (edge, auto-HTTPS) ──┐  ┌── web (React SPA, static) ──┐
│  /api /ws /healthz -> api    │─►│  mobile-first dashboard      │
│  everything else   -> web    │  └──────────────────────────────┘
│  /internal/*       -> 404    │
└──────────────────────────────┘
        Redis: runtime status, heartbeats, pub/sub      SQLite (WAL): config + history
```

Every box is a Compose service. Only Caddy (80/443), RTMP (1935/tcp) and SRT
(8890/udp) are published to the host; everything else is Compose-network only.

## Processes and responsibilities

| Service    | Image                         | Role |
|------------|-------------------------------|------|
| `caddy`    | `caddy:2-alpine`              | TLS termination, routing, blocks `/internal/*` |
| `web`      | `omnistream-web` (node build → caddy) | static dashboard build |
| `api`      | `omnistream-app`              | FastAPI: auth, REST, WebSockets, ingest auth hook |
| `relay`    | `omnistream-app` (Phase 1)    | ffmpeg supervisor; one process per enabled destination |
| `chat`     | `omnistream-app` (Phase 3)    | platform chat adapters → Redis → API WebSocket |
| `redis`    | `redis:7-alpine`              | runtime status, heartbeats, pub/sub |
| `mediamtx` | `bluenviron/mediamtx:1.21.3`  | RTMP/SRT ingest, RTSP for internal readers |
| `mobile/*` | Phase 5, `mobile` profile     | Redroid + Appium, fully optional |

`api`, `relay` and `chat` share one image (`Dockerfile` at the repo root) and one
Python code base (`core/`, `api/`, `relay/`, `chat/`); they differ only in the
command they run. Sharing the image keeps the dependency set and the encryption
key handling identical across processes.

## Python package layout

* `core/` – things every process needs and nothing that starts a server:
  * `settings.py` – `pydantic-settings`, env prefix `OMNI_`, fails fast on missing secrets.
  * `crypto.py` – `SecretBox` (Fernet / MultiFernet) for encryption at rest, key rotation.
  * `db.py` – SQLModel engine (SQLite WAL + busy timeout; Postgres-ready), `init_db`.
  * `models.py` – `Destination`, `ChatAccount`, `ChatMessage`, `OutgoingMessage`.
  * `redis.py` – client factory + the canonical key/channel names.
  * `logging.py` – structlog, JSON in production, console for humans.
* `api/` – FastAPI app factory (`create_app`), routers, auth, operator CLI.
* `relay/` – supervisor (Phase 1).
* `chat/` – adapters (Phase 3).

Enum-like columns (`platform`, `profile`, `status`) are stored as plain strings and
validated at the API boundary, so SQLite and Postgres behave the same and adding a
platform never needs a migration.

## Security model

* **Dashboard/API auth.** One admin user. Password from `OMNI_ADMIN_PASSWORD_HASH`
  (argon2id PHC string) or `OMNI_ADMIN_PASSWORD` (hashed in memory at startup).
  Successful login sets an `HttpOnly`, `SameSite=Lax` cookie containing an
  `itsdangerous` signed + timestamped token (30 days by default). `Secure` is set
  automatically when the request came in over HTTPS (Caddy sets
  `X-Forwarded-Proto`; uvicorn runs with `--proxy-headers`). Login attempts are
  rate-limited per client IP (10 per 5 minutes).
* **Ingest auth.** MediaMTX runs with `authMethod: http` and asks
  `POST api:8000/internal/mediamtx/auth` for every publish/read. Publishing is
  allowed only on `live/<OMNI_INGEST_STREAM_KEY>` (constant-time compare);
  reading only from private/Compose-network addresses (the relay). Caddy answers
  404 for `/internal/*`, so the hook is unreachable from the internet.
  (Endpoint lands in Phase 1; until then MediaMTX gets a 404 and rejects everyone.)
* **Secrets at rest.** Stream keys and OAuth tokens are Fernet-encrypted with
  `OMNI_SECRET_KEY` before they touch the DB. Column names end in `_encrypted`.
  Rotation: `OMNI_SECRET_KEY=new,old`, restart, re-save (or a future
  `omnistream rotate-keys`).
* **No secrets in git.** `.env` is ignored; `scripts/init_env.py` generates one.

## Data flow (Phase 1 preview)

1. OBS publishes to `rtmp://host/live/<key>`; MediaMTX asks the API; API says 200.
2. The relay polls the MediaMTX API every couple of seconds (`/v3/paths/list`) and
   the DB for enabled destinations, and reconciles: start an ffmpeg
   `-i rtsp://mediamtx:8554/live/<key> -c copy -f flv rtmp://platform/key` per
   destination, stop ones that were disabled, restart crashed ones with
   exponential backoff.
3. Per-destination status (`starting|live|retrying|error`, last error, bitrate,
   uptime) is written to Redis and published on `omni:events`; the API fans it out
   to dashboard WebSockets and persists `status`/`last_error` on the row.

## Local development & tests

* `make test` runs unit tests with a temp SQLite file and `fakeredis`; no Docker needed.
* `make test-integration` (Phase 1+) spins up a real `mediamtx` binary, `redis-server`
  and `ffmpeg` test patterns on random ports; skipped automatically when the
  binaries are missing.
* `make lint` = ruff (lint + format) + mypy `--strict` on everything + `tsc` for the web.
* CI (`.github/workflows/ci.yml`) runs lint/tests, the integration suite, validates
  the Compose file, and builds both images for **linux/arm64** under QEMU so an
  amd64-only dependency can never sneak in.

## Decisions log

* **MediaMTX over nginx-rtmp**: single static binary, official arm64 image, RTMP +
  SRT + RTSP in one process, HTTP auth hook, JSON control API we can poll.
* **ffmpeg subprocess per destination** (not one ffmpeg with `tee`): isolation is a
  hard rule; `tee` shares one process and one failure domain.
* **Relay polls instead of MediaMTX `runOnReady` hooks**: no curl in the MediaMTX
  image, no inbound dependency on the relay being up; a 2 s poll is cheap.
* **SQLite in WAL mode shared by api + relay** rather than an internal RPC: write
  volume is tiny (config changes, status flips), and it keeps the relay usable
  even if the API is restarting. Postgres is a URL change away.
* **Two Caddy processes** (edge + static): keeps the web image independently
  rebuildable, and the edge config is five lines.
* **Vite 7 + React 19 + TypeScript 5.9** pinned to current stable majors; no UI kit,
  plain CSS with system fonts for fast loads on a phone.
