# Platform notes

What each destination needs for RTMP relay (Phase 1) and what its chat API allows
(Phases 3/4). Rows are filled in as each integration is implemented and verified
against current docs; nothing here is a guess.

## RTMP ingest endpoints (relay)

| Platform | Server URL (`rtmp_url`)                      | Notes |
|----------|----------------------------------------------|-------|
| YouTube  | `rtmp://a.rtmp.youtube.com/live2`            | key from YouTube Studio → Go live → Stream settings. `rtmps://a.rtmps.youtube.com:443/live2` also works. |
| Twitch   | `rtmp://live.twitch.tv/app`                  | regional ingests (e.g. `rtmp://sfo.contribute.live-video.net/app`) have lower latency. |
| Facebook | `rtmps://live-api-s.facebook.com:443/rtmp`   | RTMPS required (plain RTMP is rejected). |
| Kick     | `rtmps://fa723fc1b171.global-contribute.live-video.net/app` | shown in Kick creator dashboard; RTMPS. |
| X        | `rtmp://va.pscp.tv:80/x`                     | from X Media Studio → Producer. |
| Custom   | anything ffmpeg can push to                  | `rtmp://`, `rtmps://`, `srt://` |

Stream copy (`-c copy`) is used for every row; make OBS output H.264 + AAC, which
all of the above accept. `vertical_720p` re-encodes (opt-in per destination).

Verified in Phase 1 against two local MediaMTX "fake platforms" (RTMP). RTMPS and
SRT outputs rely on the Debian `ffmpeg` build in the image (OpenSSL/GnuTLS + libsrt
are compiled in); first real-platform runs should be checked with `make logs S=relay`.

Keyframe interval: YouTube/Twitch/Facebook want a keyframe every 2 s (Kick/X are
fine with it too). Set OBS to a 2 s keyframe interval; `copy` cannot change it.

## Chat APIs (Phase 3: read; Phase 4: send)

Verified against the official docs on 2026-10-25. Each row is what the adapter in
`chat/adapters/<platform>.py` implements.

| Platform | Read | Send (Phase 4) | Auth / scopes | Notes |
|----------|------|----------------|---------------|-------|
| Twitch | EventSub **WebSocket** `wss://eventsub.wss.twitch.tv/ws`, subscription `channel.chat.message` v1 via `POST helix/eventsub/subscriptions` (`transport.method=websocket`) | `POST helix/chat/messages` (`broadcaster_id`, `sender_id`, `message`) | User token of the broadcaster: `user:read:chat` (+ `user:write:chat`). Validate hourly at `id.twitch.tv/oauth2/validate`; refresh at `oauth2/token`. | Keepalive timeout, `session_reconnect` and `revocation` handled. Max 300 subs/connection. |
| YouTube | `liveBroadcasts.list?broadcastStatus=active` (1 unit) → `snippet.liveChatId`; `liveChatMessages.list` (**5 units/call**, honours `pollingIntervalMillis`). Opt-in `streamList` (server streaming, 5 units/connection) via `OMNI_YOUTUBE_STREAM_MODE`. | `liveChatMessages.insert` (20 units) | Google OAuth, scope `youtube.force-ssl` (read+write), `access_type=offline` for a refresh token. | Default project quota is 10,000 units/day ≈ 2,000 polls. The adapter stretches the poll interval to fit `OMNI_YOUTUBE_DAILY_QUOTA × OMNI_YOUTUBE_CHAT_QUOTA_SHARE` and slows down when chat is quiet. Request a quota increase in Google Cloud for long daily streams. |
| Kick | Official **webhooks**: `POST /public/v1/events/subscriptions` for `chat.message.sent`; Kick POSTs to `https://<domain>/api/v1/webhooks/kick`, signed RSA-SHA256 over `message_id.timestamp.body` (public key from `GET /public/v1/public-key`). | `POST /public/v1/chat` (`type: user`, `broadcaster_user_id`, `content` ≤ 500 chars) | OAuth 2.1 with PKCE at `id.kick.com`; scopes `user:read channel:read chat:write events:subscribe`. | The webhook URL is set in the Kick app settings (owner task). Unverified apps may hold up to 1,000 chat subscriptions. The unofficial Pusher socket is **not** used. |
| Facebook | SSE `https://streaming-graph.facebook.com/{live-video-id}/live_comments` with a Page token; fallback polling `GET /{live-video-id}/comments?order=reverse_chronological&live_filter=no_filter` every 3 s. Live video found via `GET /{page-id}/live_videos?broadcast_status=["LIVE"]`. | Not available on the live-video comments edge per the current reference; to be verified in Phase 4 (likely `POST /{video-id}/comments`). | Facebook Login → long-lived user token → Page tokens from `/me/accounts`; permissions `pages_show_list`, `pages_read_engagement`, `pages_read_user_content`, `pages_manage_engagement`. App review needed for non-admin use. | Pasting a Page access token manually also works (`channel_ref` = Page id). |
| X | — | — | — | X has no public live chat API; chat can only be reached via Phase 5 mobile automation. |

### Sending (Phase 4)

`POST /api/v1/chat/send` with `targets: "all"` or a list of account ids. The chat
service fans out concurrently, one task per target, and records a per-target
status: `sent`, `failed` (platform error), `rate_limited` (next slot further away
than `OMNI_CHAT_SEND_MAX_WAIT`), `offline` (account not connected) or
`unsupported`. Replies: tapping a message pre-selects its account and passes the
platform message id as `reply_to`; only the owning platform receives it
(Twitch `reply_parent_message_id`, Kick `reply_to_message_id`, Facebook comments
on the comment id; YouTube has no reply primitive, so it sends a plain message).

| Platform | Documented limit | Our default (`OMNI_CHAT_RATE_LIMITS` / `_BURST`) | Max length |
|----------|------------------|--------------------------------------------------|------------|
| Twitch | 20 msgs / 30 s (100 as broadcaster, moderator or VIP); 1 msg / s / channel for non-privileged senders | 1 msg/s, burst 5 | 500 chars |
| YouTube | No per-second limit documented; each insert costs 20 quota units (500 sends on a 10,000-unit day) | 1 msg / 3 s, burst 1 | 200 chars (API) |
| Kick | Not published; the API answers 429 when exceeded | 1 msg/s, burst 3 | 500 chars / 2048 bytes |
| Facebook | Page comment publishing is subject to the general Graph API rate limits | 1 msg / 5 s, burst 1 | 8,000 chars |
| X | No API for live chat | – | – |

Facebook comments are published on the LiveVideo's underlying **Video** object
(`GET /{live-video-id}?fields=video` → `POST /{video-id}/comments`), which the
Graph reference lists as a publishable object; this path is implemented per the
docs and should be confirmed on the first real stream.

### Quota math for YouTube

`list` costs 5 units, so a 5 s poll is 3,600 units/hour: a 10,000-unit project allows
about 2.2 hours of chat per day. The adapter keeps a daily counter in Redis
(`omni:youtube:quota:<day>`) and raises the interval so the configured share lasts
until the UTC reset; the dashboard shows `waiting` with the reason when it pauses.
