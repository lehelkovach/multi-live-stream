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

## Chat APIs

Filled in during Phase 3/4.
