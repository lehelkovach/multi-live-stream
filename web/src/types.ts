export type Platform = "youtube" | "facebook" | "twitch" | "kick" | "x" | "custom";
export type Profile = "copy" | "vertical_720p" | "custom";
export type Status = "disabled" | "idle" | "starting" | "live" | "retrying" | "error";

export interface Runtime {
  status: string;
  last_error: string | null;
  pid: number | null;
  started_at: string | null;
  uptime_s: number;
  bitrate_kbps: number;
  bytes_sent: number;
  restarts: number;
  consecutive_failures: number;
  next_retry_at: string | null;
  detail: string | null;
  updated_at: string | null;
}

export interface Destination {
  id: number;
  name: string;
  platform: Platform;
  rtmp_url: string;
  enabled: boolean;
  profile: Profile;
  ffmpeg_args: string | null;
  stream_key_masked: string;
  status: Status;
  last_error: string | null;
  runtime: Runtime | null;
  created_at: string;
  updated_at: string;
}

export interface DestinationInput {
  name: string;
  platform: Platform;
  rtmp_url: string;
  enabled: boolean;
  profile: Profile;
  ffmpeg_args: string | null;
  stream_key?: string;
}

export interface IngestState {
  live: boolean;
  since: string | null;
  bitrate_kbps: number;
  tracks: string[];
  source: string | null;
  updated_at: string | null;
}

export interface RelayState {
  enabled: boolean;
  relay_alive: boolean;
  heartbeat_age_s: number | null;
  ingest: IngestState | null;
  live_count: number;
  enabled_count: number;
}

export interface IngestInfo {
  rtmp_server: string;
  srt_url: string;
  stream_key: string;
  path: string;
}

export const PLATFORMS: { value: Platform; label: string; url: string }[] = [
  { value: "youtube", label: "YouTube", url: "rtmp://a.rtmp.youtube.com/live2" },
  { value: "twitch", label: "Twitch", url: "rtmp://live.twitch.tv/app" },
  { value: "facebook", label: "Facebook", url: "rtmps://live-api-s.facebook.com:443/rtmp" },
  { value: "kick", label: "Kick", url: "" },
  { value: "x", label: "X", url: "rtmp://va.pscp.tv:80/x" },
  { value: "custom", label: "Custom RTMP/SRT", url: "" },
];

export const PROFILES: { value: Profile; label: string; hint: string }[] = [
  { value: "copy", label: "Copy (no re-encode)", hint: "Passes OBS output through untouched. Default." },
  { value: "vertical_720p", label: "Vertical 720p", hint: "Centre-crop to 9:16, re-encode 720x1280 @ 2.5 Mbps." },
  { value: "custom", label: "Custom ffmpeg args", hint: "Output options placed before the destination URL." },
];
