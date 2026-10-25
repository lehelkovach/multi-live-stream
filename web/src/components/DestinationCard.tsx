import { useState } from "react";
import { formatBitrate, formatDuration, hostOf } from "../format";
import type { Destination } from "../types";
import { StatusPill } from "./StatusPill";
import { Toggle } from "./Toggle";

interface Props {
  dest: Destination;
  busy: boolean;
  onToggle: (dest: Destination, enabled: boolean) => void;
  onEdit: (dest: Destination) => void;
  onDelete: (dest: Destination) => void;
}

const PLATFORM_LETTER: Record<string, string> = {
  youtube: "YT",
  twitch: "TW",
  facebook: "FB",
  kick: "KI",
  x: "X",
  custom: "RT",
};

export function DestinationCard({ dest, busy, onToggle, onEdit, onDelete }: Props) {
  const [showError, setShowError] = useState(false);
  const rt = dest.runtime;
  const isLive = dest.status === "live";
  const error = dest.status === "error" || dest.status === "retrying" ? dest.last_error : null;
  const hint = !isLive && !error ? rt?.detail : null;

  return (
    <article className={`card dest dest-${dest.status}`} data-testid="dest-card" data-id={dest.id}>
      <div className="dest-head">
        <span className={`avatar avatar-${dest.platform}`} aria-hidden="true">
          {PLATFORM_LETTER[dest.platform] ?? "?"}
        </span>
        <div className="dest-title">
          <div className="dest-name" data-testid="dest-name">
            {dest.name}
          </div>
          <div className="muted small">
            {hostOf(dest.rtmp_url)}
            {dest.profile !== "copy" && <> · {dest.profile}</>}
            {dest.stream_key_masked ? <> · key {dest.stream_key_masked}</> : <> · no key</>}
          </div>
        </div>
        <StatusPill status={dest.status} />
      </div>

      <div className="dest-stats">
        {isLive || dest.status === "starting" ? (
          <>
            <span data-testid="dest-bitrate">{formatBitrate(rt?.bitrate_kbps)}</span>
            <span>· up {formatDuration(rt?.uptime_s)}</span>
            {rt && rt.restarts > 0 && <span>· {rt.restarts} restarts</span>}
          </>
        ) : error ? (
          <button type="button" className="linkish error" onClick={() => setShowError((v) => !v)}>
            {showError ? error : `${error.slice(0, 90)}${error.length > 90 ? "…" : ""}`}
            {rt?.next_retry_at && !showError && <span className="muted"> · retrying</span>}
          </button>
        ) : (
          <span className="muted">{hint ?? (dest.status === "disabled" ? "switched off" : "waiting")}</span>
        )}
      </div>

      <div className="dest-actions">
        <Toggle
          checked={dest.enabled}
          disabled={busy}
          label={`Enable ${dest.name}`}
          testId="dest-toggle"
          onChange={(next) => onToggle(dest, next)}
        />
        <span className="spacer" />
        <button type="button" className="btn ghost" onClick={() => onEdit(dest)} data-testid="dest-edit">
          Edit
        </button>
        <button type="button" className="btn ghost danger" onClick={() => onDelete(dest)} data-testid="dest-delete">
          Delete
        </button>
      </div>
    </article>
  );
}
