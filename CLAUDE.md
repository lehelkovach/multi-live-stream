# CLAUDE.md — OmniStream (self-hosted multistream + unified chat)

## What we're building
A self-hosted Restream alternative running on an OCI Ampere (arm64) VM.
1. **Relay:** OBS sends ONE stream → server fans it out to many platforms (YouTube, Facebook, Twitch, Kick, X, custom RTMP).
2. **Mobile-app streaming:** reach apps with no RTMP ingest (e.g. MeetMe) by running Android in containers (Redroid) with a virtual camera fed from the stream, driven by Appium.
3. **Unified chat:** aggregate chat from every destination into one dashboard; reply to one platform or broadcast to all.

Owner is an experienced Python/systems engineer working from the cloud (often via phone). Prefer clear, boring, well-tested code over cleverness.

## Hard rules
- Target **linux/arm64**. Every image and binary must work on Ampere A1. Verify before choosing a dependency.
- Everything runs via **Docker Compose**. One `docker compose up -d` brings the stack up.
- **No secrets in git.** Use `.env` (gitignored) + `.env.example`. Stream keys and OAuth tokens are stored encrypted at rest (Fernet; key from env).
- Dashboard and API require auth (single admin user is fine to start; argon2 password hash + session cookie).
- Ingest requires a secret stream key; reject unknown publishers.
- **Never re-encode unless a destination requires it.** Default is `-c copy`. Transcoding (e.g. 9:16 vertical for mobile) is an opt-in per-destination profile.
- One failing destination must never affect the others or the ingest. Each output is an isolated, supervised process with auto-restart + exponential backoff.
- Work phase by phase. Do not start the next phase until the current one's acceptance tests pass. Commit at the end of each phase with a short summary in `docs/CHANGELOG.md`.
- Keep the Android/mobile module (Phase 5) fully isolated, as an optional Compose profile, so its breakage can't affect relay or chat.

## Stack
- **Ingest:** MediaMTX (RTMP :1935, SRT :8890), auth hook → our API.
- **Relay workers:** Python supervisor managing one `ffmpeg` process per enabled destination, pulling from MediaMTX.
- **Backend:** Python 3.12, FastAPI, SQLModel + SQLite (Postgres-ready), Redis (pub/sub + state), WebSockets.
- **Frontend:** Vite + React + TypeScript, mobile-first (owner uses a phone), served by the backend or Caddy.
- **Reverse proxy/TLS:** Caddy with automatic HTTPS.
- **Mobile module:** Redroid (Android container, arm64), Appium + UiAutomator2, virtual-camera approach TBD in Phase 5 spike.
- **Testing:** pytest, ruff, mypy (strict on core), Playwright for one dashboard smoke test.
- **Tooling:** `Makefile` with `make up`, `make down`, `make test`, `make lint`, `make logs`.

## Repo layout
```
/ingest        MediaMTX config
/relay         ffmpeg supervisor (python package)
/api           FastAPI app (destinations, auth, chat, websockets)
/chat          platform chat adapters (one module per platform)
/web           React dashboard
/mobile        Redroid + Appium automation (optional profile)
/deploy        Caddyfile, OCI notes, firewall/security-list notes
/docs          ARCHITECTURE.md, CHANGELOG.md, platform notes
/tests         unit + integration
docker-compose.yml, .env.example, Makefile
```

## Core data model (starting point)
- `Destination`: id, name, platform, rtmp_url, stream_key (encrypted), enabled, profile (`copy` | `vertical_720p` | custom), status, last_error.
- `ChatAccount`: id, platform, credentials/tokens (encrypted), channel/stream ref, enabled.
- `ChatMessage` (normalized): id, platform, destination_id, author, author_id, text, timestamp, is_own, raw (json).
- `OutgoingMessage`: text, targets (list of platforms or "all"), per-target status.

## Phases

### Phase 0 — Skeleton
Compose stack with MediaMTX, API, Redis, web, Caddy. Health endpoints. `.env.example`. CI-style `make test` + `make lint`.
**Accept:** `make up` works on arm64; `/healthz` returns ok; lint + tests pass.

### Phase 1 — Relay
- Ingest auth via stream key.
- Destinations from DB; supervisor starts/stops ffmpeg per destination when a stream goes live/offline or a destination is toggled.
- Auto-restart with backoff; status + last error per destination exposed via API.
- Optional transcode profiles (`vertical_720p` crop/scale).
**Accept:** integration test publishes a test pattern (`ffmpeg -f lavfi testsrc`) to ingest and verifies it arrives at 2 local MediaMTX "fake destinations"; killing one destination's process doesn't affect the other; wrong stream key is rejected.

### Phase 2 — Dashboard
Mobile-first UI: login, add/edit/toggle destinations, live status (WebSocket), per-destination bitrate/uptime, start/stop all.
**Accept:** Playwright smoke test: login → add destination → toggle → status updates.

### Phase 3 — Chat aggregation (read)
Adapter interface:
```python
class ChatAdapter(Protocol):
    platform: str

    async def connect(self) -> None: ...
    async def messages(self) -> AsyncIterator[ChatMessage]: ...
    async def send(self, text: str) -> SendResult: ...
    async def close(self) -> None: ...
```
Adapters in order: **Twitch** (IRC/EventSub), **YouTube** (Live Chat API, respect quota, adaptive polling), **Kick**, **Facebook** (Graph API comments). Normalize → Redis pub/sub → WebSocket → unified chat pane with platform badges. Persist history.
**Accept:** unit tests with recorded fixtures per adapter; a fake adapter feeds the UI in an integration test; one adapter dying is logged and retried without affecting others.

### Phase 4 — Replies + broadcast
Compose box in the dashboard: reply to one platform, a selected set, or all. Per-target delivery status. Rate-limit per platform. Document what each platform's API allows in `docs/platforms.md` (some can't post).
**Accept:** tests prove fan-out to fake adapters, partial failure reporting, and rate limiting.

### Phase 5 — Mobile apps (spike first, then build)
**5a spike (write findings to `docs/mobile-spike.md` before building):**
- Get Redroid running on arm64 OCI (kernel modules: binder/ashmem; document exact steps).
- Find a working way to feed the RTMP stream as the camera inside Redroid (virtual camera module, v4l2loopback, or patched camera HAL). Compare options.
- Check target apps for emulator/root detection and note their terms-of-service risk.
- Fallback documented: real Android phone(s) connected via ADB over Tailscale using the same automation.

**5b build (only if spike succeeds):**
- One Redroid container per mobile account/app, under a `mobile` Compose profile.
- Appium flows per app: launch, go live, keep alive, end stream.
- **Chat read:** read the accessibility/UI tree via UiAutomator2 (OCR only as fallback) → normalized `ChatMessage`s.
- **Chat send:** type into the app's chat box.
- Plug into the same adapter interface so the dashboard treats it like any other platform.
- Screenshot-on-failure + health checks; auto-recover flows.
**Accept:** start with ONE target app; go live from the dashboard, show its chat in the unified pane, send one reply.

### Phase 6 — Hardening
Prometheus metrics + simple Grafana board (or a built-in stats page), structured logs, backups of DB/secrets, OCI firewall/security-list docs (open 443, 1935/8890 restricted to owner IP if possible), egress bandwidth estimate in docs.

## Working agreement for Claude Code
- Start each phase by writing/updating a short plan in `docs/ARCHITECTURE.md`, then implement.
- Write tests alongside code; run `make test` and `make lint` before declaring a phase done.
- If a platform API or tool behaves differently than assumed, research current docs, update `docs/platforms.md`, and adapt. Don't hardcode guesses.
- When blocked by something only the owner can do (OAuth app registration, API keys, OCI console settings), list it clearly in `docs/TODO-owner.md` and keep working on whatever isn't blocked.
- Keep the README current: setup on a fresh OCI Ampere VM in under 10 commands.
