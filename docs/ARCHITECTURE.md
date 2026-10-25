# OmniStream architecture

Status: Phases 0-4 and 6 complete; Phase 5 is a documented spike (`docs/mobile-spike.md`) awaiting hands-on results on the VM. This document is updated at the start of
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

## Relay (Phase 1)

### Data flow

1. OBS publishes to `rtmp://host:1935/live/<key>` (or SRT). MediaMTX POSTs the
   request to `api:/internal/mediamtx/auth`; the API compares the path against
   `live/<OMNI_INGEST_STREAM_KEY>` in constant time and answers 200/401. On 200 it
   also publishes `ingest.publish` so the relay reacts without waiting for a poll.
2. `python -m relay` (`relay/supervisor.py`) runs a reconcile loop every
   `OMNI_RELAY_POLL_INTERVAL` (2 s) or immediately on a Redis event
   (`destinations.changed`, `relay.changed`, `ingest.publish`). Each pass:
   * asks MediaMTX (`/v3/paths/list`) whether `live/<key>` is ready,
   * loads the global switch (`app_setting.relay.enabled`) and all destinations,
   * computes the desired set: every enabled destination, if the input is live and
     the switch is on. For each it builds the ffmpeg argv (`relay/ffmpeg.py`),
   * stops workers that are no longer desired or whose argv changed (key/URL/profile
     edits restart just that output), starts missing ones,
   * writes a status blob per destination to Redis (TTL 30 s) and the ingest
     state, and bumps the heartbeat.
3. A `DestinationWorker` (`relay/worker.py`) owns one ffmpeg process:
   `-rtsp_transport tcp -timeout 10s -i rtsp://mediamtx:8554/live/<key>` then the
   profile args then `-f flv rtmp://platform/app/key`. It parses `-progress pipe:1`
   for bytes/bitrate, flips `starting -> live` on the first output bytes, keeps the
   last 30 stderr lines for `last_error`, kills the process if no progress arrives
   for `OMNI_RELAY_STALL_TIMEOUT` (20 s), and restarts on exit with exponential
   backoff (1 s, 2 s, 4 s ... capped at 60 s, +20% jitter). After
   `OMNI_RELAY_ERROR_AFTER_FAILURES` (5) consecutive quick failures the status is
   `error` but it keeps retrying at the cap; a run longer than
   `OMNI_RELAY_HEALTHY_AFTER` (30 s) resets the counter. Children get
   `PR_SET_PDEATHSIG`, so a crashed relay can never leave an orphan ffmpeg pushing
   while its replacement starts another.
4. The API merges the DB row with the Redis blob (`GET /api/v1/destinations`),
   exposes the global switch and ingest state (`/api/v1/relay`), and the OBS
   settings (`/api/v1/relay/ingest`). Status changes also persist to
   `destination.status` / `last_error` so history survives a Redis restart.

### Isolation guarantees

* One asyncio task + one OS process (own session) per destination; a worker
  failure only touches its own status blob.
* The supervisor never awaits a worker inside the reconcile pass other than
  `stop()` (bounded by a 5 s grace + SIGKILL).
* Ingest is a different process (MediaMTX) and never depends on the relay.
* Verified by `tests/integration/test_relay_e2e.py` (real MediaMTX + ffmpeg): SIGKILL
  of one output's ffmpeg leaves the other output's byte counter rising, and the
  killed one is back within seconds with `restarts == 1`.

### Profiles

| profile         | what ffmpeg does |
|-----------------|------------------|
| `copy` (default)| `-c copy`; zero CPU; needs H.264 + AAC from OBS for RTMP targets |
| `vertical_720p` | centre crop to 9:16, scale 720x1280, libx264 veryfast 2.5 Mbps, 2 s keyframes, AAC 128k |
| `custom`        | operator supplied output args (`Destination.ffmpeg_args`) |

Output container is chosen from the URL: `rtmp(s)://` -> FLV, `srt://` / `udp://`
-> MPEG-TS.

### Status model

`disabled` (enabled=false) · `idle` (enabled, waiting; `detail` says why: no input
stream / relay stopped) · `starting` · `live` · `retrying` (with `next_retry_at`) ·
`error` (many failures, still retrying, or invalid config such as an undecryptable
key).

## Dashboard (Phase 2)

Vite + React 19 + TypeScript, no UI framework, plain CSS. One screen, built for a
phone: a sticky header (live-updates badge, sign out), an **input/relay panel**
(ingest live/offline + bitrate + uptime, Start all / Stop all, collapsible OBS
settings with copy buttons) and the **destination list**. Each card shows the
platform avatar, name, host, masked key, a status pill, bitrate/uptime/restarts
while live or the last error while failing, an enable toggle, Edit and Delete.
Add/Edit is a bottom sheet with platform presets that pre-fill the server URL.

### Live updates

`GET /ws/events` (cookie-authenticated; closes with 4401 otherwise) sends a
`snapshot` (destinations + relay state) on connect and then forwards every JSON
event from the Redis `omni:events` channel verbatim:

| event                 | producer | dashboard reaction |
|-----------------------|----------|--------------------|
| `destination.status`  | relay    | merge runtime + status into the card (status change) |
| `destination.stats`   | relay    | merge runtime (bitrate/uptime), ~every 2 s per live output |
| `ingest.state`        | relay    | update the input panel |
| `destinations.changed`| api      | re-fetch the destination list (REST) |
| `relay.changed`       | api      | re-fetch relay state (REST) |

The client (`web/src/hooks/useEvents.ts`) reconnects with backoff (1 s ... 10 s),
re-syncs from REST after each connect and every 30 s, and answers `pong` pings.
A disabled destination always renders `disabled` even if a stale runtime blob
says otherwise.

### Smoke test

`make test-e2e` builds the dashboard and runs `web/e2e/dashboard.spec.ts` with
Playwright (Pixel 7 emulation) against `tests/e2e/serve.py`: redis-server + the
API serving `web/dist` + the **real** relay supervisor wired to a fake MediaMTX
and fake workers (`/__test/ingest/on|off` flips the fake input). The test covers
wrong password, login, add (with platform preset), validation error, toggle
off/on, input going live -> card live with bitrate pushed over the WebSocket,
Stop all / Start all, input offline -> idle, edit keeping the key, OBS settings,
reload keeping the session, delete, sign out.

## Chat aggregation (Phase 3)

```
 Twitch EventSub WS ─┐
 YouTube Data API  ──┤  chat/adapters/*  ──►  ChatManager (python -m chat)
 Kick webhooks ──► api ──► Redis omni:webhooks:kick ──┘        │
 Facebook SSE/poll ──┘                                         ▼
                                     ChatStore: chat_message table (dedupe on
                                     platform:id) + PUBLISH omni:chat
                                                               │
                                     api /ws/events ◄──────────┘ ──► dashboard Chat tab
```

* **Adapter contract** (`chat/base.py`): `connect()` / `messages()` / `send()` /
  `close()`, exceptions tell the manager *why* a stream stopped: `NotLiveError`
  (no broadcast; retry in 60 s while our ingest is live, 5 min otherwise),
  `AuthError` and `ConfigError` (owner must act; retry every 5 min), anything
  else backs off 2 s → 120 s. One runner task per account, so one platform can
  never block another.
* **Credentials** are a JSON blob (`access_token`, `refresh_token`, `expires_at`,
  `extra`) Fernet-encrypted in `chat_account.credentials_encrypted`. Adapters
  refresh tokens themselves and persist them through a callback that does not
  bump `updated_at` (which is what the manager watches for config changes).
* **OAuth** (`chat/oauth.py`): `GET /api/v1/chat/oauth/{platform}/start` returns the
  authorize URL (state + PKCE verifier kept in Redis for 10 min);
  `/callback` exchanges the code, looks up the identity (user id / channel /
  pages) and upserts the account by `(platform, external_id)`.
* **Kick** has no socket in its public API; it pushes signed webhooks. The API
  verifies the signature (RSA PKCS#1 v1.5 / SHA-256, key fetched from Kick and
  cached, re-fetched once on failure), dedupes by message id (Redis SET NX, 1 h)
  and re-publishes the payload; the Kick adapter filters by broadcaster id.
* **YouTube quota**: polling costs 5 units; the adapter keeps a per-day counter in
  Redis and paces itself to the configured share of the daily quota.
* **History**: `GET /api/v1/chat/messages?limit=&before=&platform=`; the Chat tab
  merges the last 200 with live socket messages and dedupes by id.

## Replies and broadcast (Phase 4)

`POST /api/v1/chat/send` writes an `outgoing_message` row (text, targets,
`per_target_status` pre-filled with `pending` for every enabled target) and
publishes `chat.send`. The chat manager's `Outbox` loads the row and delivers to
each target in its own task: look up the running adapter (`offline` if there is
none), reserve a token in that account's bucket (`rate_limited` if the wait would
exceed `OMNI_CHAT_SEND_MAX_WAIT`), call `adapter.send()` under a timeout, then
write the per-target entry and publish `chat.outgoing`. When all targets are
done the summary status (`sent` / `partial` / `failed`) is stored and a final
`chat.outgoing` event with `done: true` is published. The dashboard tracks the
row it created and updates ticks per platform as events arrive; platform echoes
of our own message arrive through the normal read path as `is_own`.

## Operations (Phase 6)

* **Metrics**: `GET /metrics` builds a fresh Prometheus registry per scrape from
  Redis runtime blobs + two small DB aggregates, so there is no metrics state to
  keep in sync and a dead relay/chat process shows as
  `omnistream_service_heartbeat_age_seconds{service=...}` growing (or `+Inf`).
  Auth: dashboard cookie or `Authorization: Bearer $OMNI_METRICS_TOKEN`.
  `GET /api/v1/stats` is the JSON twin for the dashboard.
* **Logs**: every process logs structured JSON to stdout (`OMNI_LOG_FORMAT=json`);
  `make logs S=relay` to follow one service, `docker compose logs --since 1h api`
  for a window. Events carry `event`, `level`, `logger`, `timestamp` and context
  keys such as `dest_id` / `account_id`.
* **Backups**: `omnistream backup` uses SQLite's online backup API (consistent
  while the services run) into `/data/backups`, keeping `OMNI_BACKUP_KEEP`
  snapshots; `make backup` copies them to `./backups/` on the host. The `.env`
  file is part of the backup set because `OMNI_SECRET_KEY` decrypts stored
  credentials. Restore = stop the stack, copy a snapshot over
  `/data/omnistream.db` in the `app_data` volume, start.
* **Retention**: `omnistream prune` deletes chat history and outgoing messages
  older than `OMNI_CHAT_RETENTION_DAYS`.
* **Network**: see `deploy/README.md` for the OCI security list + host firewall
  rules and the egress estimate.

## Local development & tests

* `make test` runs unit tests with a temp SQLite file and `fakeredis`; no Docker needed.
* `make test-integration` (Phase 1+) spins up a real `mediamtx` binary, `redis-server`
  and `ffmpeg` test patterns on random ports; skipped automatically when the
  binaries are missing.
* `make test-e2e` runs the Playwright smoke test (needs `redis-server` + Chromium).
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
