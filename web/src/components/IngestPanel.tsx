import { useEffect, useState } from "react";
import { relay as relayApi } from "../api";
import { formatBitrate, formatDuration, sinceSeconds } from "../format";
import type { IngestInfo, RelayState } from "../types";

interface Props {
  relay: RelayState | null;
  busy: boolean;
  onToggleRelay: (enabled: boolean) => void;
}

export function IngestPanel({ relay, busy, onToggleRelay }: Props) {
  const [showObs, setShowObs] = useState(false);
  const [info, setInfo] = useState<IngestInfo | null>(null);
  const [reveal, setReveal] = useState(false);
  const [, tick] = useState(0);

  useEffect(() => {
    const t = window.setInterval(() => tick((n) => n + 1), 1000);
    return () => window.clearInterval(t);
  }, []);

  useEffect(() => {
    if (showObs && !info) relayApi.ingest().then(setInfo).catch(() => setInfo(null));
  }, [showObs, info]);

  const ingest = relay?.ingest;
  const live = Boolean(ingest?.live);
  const relayDown = relay !== null && !relay.relay_alive;

  return (
    <section className="card ingest">
      <div className="ingest-row">
        <div>
          <div className="label">Input</div>
          <div className="ingest-status" data-testid="ingest-status" data-live={live}>
            {live ? (
              <>
                <span className="dot" /> live · {formatBitrate(ingest?.bitrate_kbps)} · {formatDuration(sinceSeconds(ingest?.since))}
              </>
            ) : (
              "offline · waiting for OBS"
            )}
          </div>
          {live && ingest?.tracks?.length ? <div className="muted small">{ingest.tracks.join(" + ")}</div> : null}
        </div>
        <div className="ingest-right">
          <div className="label">Relay</div>
          <button
            type="button"
            className={`btn ${relay?.enabled ? "danger-solid" : "primary"}`}
            disabled={busy || relay === null}
            onClick={() => onToggleRelay(!relay?.enabled)}
            data-testid="relay-toggle"
          >
            {relay?.enabled ? "Stop all" : "Start all"}
          </button>
          <div className="muted small" data-testid="relay-summary">
            {relay ? `${relay.live_count}/${relay.enabled_count} live` : "…"}
            {relayDown && <span className="error"> · relay process down</span>}
          </div>
        </div>
      </div>

      <button type="button" className="linkish" onClick={() => setShowObs((v) => !v)} data-testid="obs-toggle">
        {showObs ? "Hide OBS settings" : "OBS settings"}
      </button>
      {showObs && (
        <dl className="kv" data-testid="obs-settings">
          <dt>Server</dt>
          <dd>
            <code>{info?.rtmp_server ?? "…"}</code> <CopyButton text={info?.rtmp_server} />
          </dd>
          <dt>Stream key</dt>
          <dd>
            <code>{info ? (reveal ? info.stream_key : "••••••••••••") : "…"}</code>{" "}
            <button type="button" className="btn ghost" onClick={() => setReveal((v) => !v)}>
              {reveal ? "hide" : "show"}
            </button>{" "}
            <CopyButton text={info?.stream_key} />
          </dd>
          <dt>SRT (alt.)</dt>
          <dd>
            <code className="wrap">{info?.srt_url ?? "…"}</code> <CopyButton text={info?.srt_url} />
          </dd>
        </dl>
      )}
    </section>
  );
}

function CopyButton({ text }: { text: string | undefined }) {
  const [done, setDone] = useState(false);
  if (!text || !navigator.clipboard) return null;
  return (
    <button
      type="button"
      className="btn ghost"
      onClick={() => {
        void navigator.clipboard.writeText(text).then(() => {
          setDone(true);
          window.setTimeout(() => setDone(false), 1200);
        });
      }}
    >
      {done ? "copied" : "copy"}
    </button>
  );
}
