/**
 * Live dashboard state: REST for the initial/corrective loads, a WebSocket on
 * /ws/events for pushes from the relay and the API. Reconnects with backoff and
 * re-syncs from REST after every (re)connect and every 30 s as a safety net.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { destinations as destApi, relay as relayApi } from "../api";
import type { Destination, RelayState, Runtime, Status } from "../types";

export type ConnState = "connecting" | "connected" | "offline";

interface EventsState {
  destinations: Destination[];
  relay: RelayState | null;
  conn: ConnState;
  error: string | null;
}

type Snapshot = { type: "snapshot"; destinations: Destination[]; relay: RelayState };
type DestEvent = { type: "destination.status" | "destination.stats"; id: number } & Partial<Runtime>;
type AnyEvent =
  | Snapshot
  | DestEvent
  | { type: "destinations.changed" }
  | { type: "relay.changed"; enabled: boolean }
  | { type: "ingest.state"; live: boolean; since: string | null; bitrate_kbps: number; tracks: string[]; source: string | null }
  | { type: string };

function mergeRuntime(dest: Destination, evt: DestEvent): Destination {
  const runtime: Runtime = {
    status: evt.status ?? dest.runtime?.status ?? dest.status,
    last_error: evt.last_error ?? null,
    pid: evt.pid ?? null,
    started_at: evt.started_at ?? null,
    uptime_s: evt.uptime_s ?? 0,
    bitrate_kbps: evt.bitrate_kbps ?? 0,
    bytes_sent: evt.bytes_sent ?? 0,
    restarts: evt.restarts ?? 0,
    consecutive_failures: evt.consecutive_failures ?? 0,
    next_retry_at: evt.next_retry_at ?? null,
    detail: evt.detail ?? null,
    updated_at: evt.updated_at ?? null,
  };
  const status: Status = dest.enabled ? (runtime.status as Status) : "disabled";
  return { ...dest, runtime, status, last_error: runtime.last_error ?? dest.last_error };
}

export function useEvents(enabled: boolean) {
  const [state, setState] = useState<EventsState>({ destinations: [], relay: null, conn: "connecting", error: null });
  const wsRef = useRef<WebSocket | null>(null);
  const attemptRef = useRef(0);
  const stoppedRef = useRef(false);

  const refresh = useCallback(async () => {
    try {
      const [destinations, relay] = await Promise.all([destApi.list(), relayApi.state()]);
      setState((s) => ({ ...s, destinations, relay, error: null }));
    } catch (err) {
      setState((s) => ({ ...s, error: err instanceof Error ? err.message : String(err) }));
    }
  }, []);

  useEffect(() => {
    if (!enabled) return;
    stoppedRef.current = false;
    let timer: number | undefined;

    const connect = () => {
      if (stoppedRef.current) return;
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(`${proto}//${location.host}/ws/events`);
      wsRef.current = ws;
      setState((s) => ({ ...s, conn: "connecting" }));

      ws.onopen = () => {
        attemptRef.current = 0;
        setState((s) => ({ ...s, conn: "connected" }));
      };
      ws.onmessage = (msg) => {
        if (msg.data === "pong") return;
        let evt: AnyEvent;
        try {
          evt = JSON.parse(msg.data as string) as AnyEvent;
        } catch {
          return;
        }
        switch (evt.type) {
          case "snapshot": {
            const snap = evt as Snapshot;
            setState((s) => ({ ...s, destinations: snap.destinations, relay: snap.relay, error: null }));
            break;
          }
          case "destination.status":
          case "destination.stats": {
            const de = evt as DestEvent;
            setState((s) => {
              const idx = s.destinations.findIndex((d) => d.id === de.id);
              if (idx < 0) return s;
              const next = s.destinations.slice();
              next[idx] = mergeRuntime(next[idx] as Destination, de);
              const live_count = next.filter((d) => d.status === "live").length;
              return { ...s, destinations: next, relay: s.relay ? { ...s.relay, live_count } : s.relay };
            });
            break;
          }
          case "ingest.state": {
            const ie = evt as Extract<AnyEvent, { type: "ingest.state" }>;
            setState((s) =>
              s.relay
                ? { ...s, relay: { ...s.relay, relay_alive: true, ingest: { ...ie, updated_at: new Date().toISOString() } } }
                : s,
            );
            break;
          }
          case "destinations.changed":
          case "relay.changed":
            void refresh();
            break;
          default:
            break;
        }
      };
      ws.onclose = () => {
        wsRef.current = null;
        if (stoppedRef.current) return;
        setState((s) => ({ ...s, conn: "offline" }));
        const delay = Math.min(10_000, 1000 * 2 ** attemptRef.current++);
        timer = window.setTimeout(connect, delay);
      };
      ws.onerror = () => ws.close();
    };

    void refresh();
    connect();
    const ping = window.setInterval(() => wsRef.current?.readyState === WebSocket.OPEN && wsRef.current.send("ping"), 25_000);
    const resync = window.setInterval(() => void refresh(), 30_000);
    return () => {
      stoppedRef.current = true;
      window.clearInterval(ping);
      window.clearInterval(resync);
      if (timer) window.clearTimeout(timer);
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [enabled, refresh]);

  return { ...state, refresh };
}
