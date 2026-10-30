# Changelog

One entry per completed phase (plus notable fixes). Newest first.

## Oracle Cloud deployment tooling (2026-10-30)

* `deploy/oci/provision.py`: creates (or reuses) a dedicated VCN, public subnet,
  internet gateway, route and security list and an Ampere A1 VM through the OCI
  API; `status --follow` reads bootstrap progress from the serial console, so
  no SSH access is needed.
* `deploy/oci/cloud-init.sh`: host firewall, Docker, clone, `.env` (admin password
  injected only as an argon2 hash), `docker compose up -d --build`, health wait,
  RTMP ingest smoke test, Let's Encrypt via `<ip>.sslip.io`.
* `provision.py down --yes [--network]` tears it down again, deleting only
  resources tagged as created by the script; `up --password-file` saves the
  generated admin password before launch.
* `up --ingest-cidr` on an existing VM updates the security list in place; a
  plain re-run keeps the current rules, so it can't reopen a restricted ingest.
* First real **linux/arm64** run (VM.Standard.A1.Flex, Ubuntu 24.04 aarch64): the
  whole stack built and came up healthy, wrong stream key rejected, ingest went
  live and the relay saw it, edge `/healthz` ok over a trusted certificate.
* Fixed: an unsafe lint autofix had removed every `print()` from the
  provisioner, so `up` ran silently; `deploy/` is now exempt from the no-print
  rule and a test pins the operator-facing output.
* Fixed: an argon2 hash pasted into `.env` without single quotes is mangled by
  Docker Compose's `$` expansion, so the admin login always failed. The API now
  refuses to start with a clear message, `make hash-password` prints a quoted
  line ready to paste, and `.env.example` says so.

## Full-stack verification + fixes (2026-10-28)

The real `docker compose up -d` stack was run end to end for the first time (in
the development sandbox; linux/amd64 — arm64 was first run on OCI, above):
all services healthy, HTTPS + HTTP redirect through Caddy, `/internal/*` blocked
at the edge, Secure/HttpOnly session cookie, WebSocket through the edge, RTMP and
SRT ingest with good and wrong keys, relay to a fake platform container on the
Compose network, SIGKILL of an output's ffmpeg recovered, chat read + broadcast
across containers, `make check-config`, `make backup`, data surviving down/up.
It exposed these bugs, all fixed with tests:

* **`/metrics` returned the dashboard HTML through the edge**: Caddy only routed
  `/api`, `/ws` and `/healthz` to the API. Added; a new test fails if any
  top-level API route is not proxied by `deploy/Caddyfile`.
* **The documented restore broke the database**: copying a backup into the volume
  as root left a file the app user (uid 10001) could not write, so every change
  returned 500 while health checks stayed green. Added `omnistream restore` /
  `make restore FILE=...` (validates, keeps a pre-restore copy, fixes ownership,
  clears stale WAL files, always restarts the services); backups are now
  self-contained rollback-journal files; `/api/v1/health` and a startup log line
  report a read-only database.
* **Stream end looked like a failure**: ffmpeg's clean exit when the publisher
  disconnects was reported as "ffmpeg exited with code 0" and followed by a blind
  retry that failed with RTSP 404. It is now "input stream ended", not counted as
  a failure, and the worker waits for the supervisor (stop if offline, immediate
  restart if the publisher is back).
* **Every relay error message was wrapped in shell quotes** (it went through
  `shlex.join`); secrets are now redacted from the plain text.
* **YouTube chat polled every ~22 s most of the day**: the quota pacer spread the
  daily budget evenly until midnight UTC. It now keeps enough budget for
  `OMNI_YOUTUBE_CHAT_HOURS` (default 3) of polling, about 7 s per poll on a default
  project, stretching only if the stream runs longer.
* **Dashboard race**: a delivery or account-status event arriving just before the
  browser handled the matching API response could be lost (a send stayed
  "pending"); early events are now buffered in React state with pure updaters.
* **Fake chat adapter ids collided** after an account was deleted and re-created
  (SQLite reuses row ids), so new messages were deduplicated away. Ids now carry a
  per-instance token. The Playwright chat flow uses unique names per run and
  passes 10/10 when repeated on one server.

## Phase 6 - Hardening (2026-10-25)

* `GET /metrics` (Prometheus text format; dashboard session or
  `OMNI_METRICS_TOKEN` bearer): ingest live/bitrate, service heartbeat ages,
  per-destination status/bitrate/uptime/restarts, chat account status, chat
  messages and outgoing sends today, YouTube quota units spent today.
  `GET /api/v1/stats` returns the same data as JSON.
* `omnistream backup` (SQLite online backup with rotation) and `make backup`
  (copies snapshots to `./backups/`); `omnistream prune` / `make prune` for chat
  history retention (`OMNI_CHAT_RETENTION_DAYS`, default 30).
* deploy/README: OCI security list + host firewall checklist, backup cron, egress
  estimate table, log inspection.

## Phase 5a - Mobile spike document (2026-10-25)

* `docs/mobile-spike.md`: desk research + VM runbook for Redroid on arm64 OCI
  (kernel modules, `_64only` images, memfd), a comparison of virtual-camera
  options, per-app detection/ToS checklist, ADB-over-Tailscale fallback and the
  go/no-go criteria. Hands-on findings are left for the owner to fill in on the
  VM; nothing in `mobile/` is built until then.

## Phase 4 - Replies + broadcast (2026-10-25)

* `POST /api/v1/chat/send` (`targets: "all" | [ids]`, optional `reply_to`) queues an
  `outgoing_message`; the chat service fans out concurrently with one task per
  target, per-account token-bucket rate limits with per-platform defaults,
  per-target timeouts, and records `sent / failed / rate_limited / offline /
  unsupported` per target plus a `sent / partial / failed` summary. Progress is
  broadcast as `chat.outgoing` events; `GET /api/v1/chat/outgoing` lists history.
* Adapters gained `reply_to`: Twitch `reply_parent_message_id`, Kick
  `reply_to_message_id`, Facebook comment-on-comment; Facebook sending implemented
  as a Page comment on the live video's Video object.
* Dashboard: compose bar in the Chat tab with All / per-account target chips,
  tap-to-reply, and an outgoing row showing a tick/cross per platform with the
  error on tap.
* Tests: token bucket, fan-out with fake adapters (partial failure, offline
  target, events), rate limiting, reply scoping, API validation, Playwright
  compose flow.

## Phase 3 - Chat aggregation, read side (2026-10-25)

* `chat/` package: `ChatAdapter` protocol, normalized `ChatMessage`, encrypted
  `Credentials`, OAuth helpers (authorize/exchange/refresh/identity) for Twitch,
  Google/YouTube, Kick (PKCE) and Facebook (long-lived Page tokens).
* Adapters: Twitch (EventSub WebSocket, hourly token validation, reconnect and
  revocation handling), YouTube (broadcast lookup, quota-aware adaptive polling,
  experimental streamList), Kick (signed webhooks relayed through Redis, automatic
  event subscription), Facebook (SSE live_comments with polling fallback), and a
  fake adapter for tests/demos.
* `python -m chat` manager (new `chat` Compose service): one supervised runner per
  enabled account; NotLive/Auth/Config/transient failures handled per account with
  the right wait; wakes on ingest-live and account-change events.
* Persistence + fan-out: messages stored once (`chat_message`), published on Redis,
  forwarded over `/ws/events`; account status blobs + events like destinations.
* API: `/api/v1/chat/{platforms,accounts,messages}`, OAuth start/callback, public
  signature-verified `POST /api/v1/webhooks/kick` with replay protection.
* Dashboard: Stream / Chat / Accounts tabs; unified chat pane with platform tags,
  badges, filter chips, auto-scroll; Accounts tab with Connect (OAuth) and Token
  (manual) flows, status pills, enable toggles, Facebook page picker.
* Tests: recorded fixtures per adapter (Twitch notification, YouTube list page,
  Kick webhook payload, Facebook comments), a local EventSub websocket server,
  mocked HTTP transports, Kick signature round-trip, manager isolation (one failing
  account never affects another), API tests, Playwright chat flow with the fake
  adapter.
* Docs: platform table with verified endpoints/scopes/quota math, owner TODOs for
  the four OAuth apps and the Kick webhook URL.

## Phase 2 - Dashboard (2026-10-25)

* Mobile-first React dashboard: login, destination cards (status pill, bitrate,
  uptime, restarts, last error), add/edit bottom sheet with platform presets,
  enable toggles, delete, Start all / Stop all, input panel with OBS settings.
* `GET /ws/events` WebSocket: snapshot on connect, then relay/API events forwarded
  from Redis; client reconnects with backoff and re-syncs from REST.
* Shared read models (`api/services/state.py`) used by REST and WebSocket.
* `init_db` tolerates the api/relay start-up race on a fresh SQLite file.
* Playwright smoke test (`make test-e2e`, CI job `e2e`) against a harness that runs
  the real relay supervisor with a fake ingest: login -> add destination -> toggle
  -> live status pushed over the WebSocket, plus edit/delete/sign-out.

## Phase 1 - Relay (2026-10-25)

* Ingest auth: MediaMTX HTTP hook (`/internal/mediamtx/auth`) accepts publishes only
  on `live/<OMNI_INGEST_STREAM_KEY>` (constant-time compare, per-IP rate limit) and
  reads only from internal networks. Blocked at the Caddy edge.
* Destinations API (`/api/v1/destinations`): CRUD with Fernet-encrypted stream keys,
  masked in responses, validation of URLs/platforms/profiles, change events to Redis.
* Relay supervisor (`python -m relay`, new `relay` Compose service): reconcile loop
  (poll + Redis wake-ups), one supervised ffmpeg per enabled destination,
  exponential backoff with jitter, stall watchdog, PDEATHSIG, per-destination
  status/bitrate/uptime/last_error in Redis + DB, heartbeat + `--healthcheck`.
* Profiles: `copy` (default, never re-encodes), `vertical_720p`, `custom`.
* Global start/stop switch + ingest state + OBS settings (`/api/v1/relay`).
* Tests: 70+ unit tests (worker against a fake ffmpeg, supervisor against fake
  MediaMTX/workers, API) and an end-to-end integration test with real MediaMTX,
  ffmpeg and redis-server proving fan-out to two fake destinations, failure
  isolation (SIGKILL one output, the other keeps flowing), wrong-key rejection,
  toggles and the global switch. CI runs it.
* MediaMTX config: `moq: no` (1.21 default listener + cert generation not needed).

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
