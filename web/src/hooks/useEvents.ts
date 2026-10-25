/**
 * Live dashboard state: REST for the initial/corrective loads, a WebSocket on
 * /ws/events for pushes from the relay and the API. Reconnects with backoff and
 * re-syncs from REST after every (re)connect and every 30 s as a safety net.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { chat as chatApi, destinations as destApi, relay as relayApi } from "../api";
import type { ChatAccount, ChatMessage, Destination, RelayState, Runtime, Status } from "../types";

export type ConnState = "connecting" | "connected" | "offline";

interface EventsState {
  destinations: Destination[];
  relay: RelayState | null;
  accounts: ChatAccount[];
  chat: ChatMessage[]; // live messages received over the socket (bounded)
  conn: ConnState;
  error: string | null;
}

const MAX_LIVE_CHAT = 500;

type Snapshot = { type: "snapshot"; destinations: Destination[]; relay: RelayState };
type DestEvent = { type: "destination.status" | "destination.stats"; id: number } & Partial<Runtime>;
type AnyEvent =
  | Snapshot
  | DestEvent
  | { type: "destinations.changed" }
  | { type: "relay.changed"; enabled: boolean }
  | { type: "ingest.state"; live: boolean; since: string | null; bitrate_kbps: number; tracks: string[]; source: string | null }
  | ({ type: "chat.message" } & ChatMessage)
  | { type: "chat.account.status"; id: number; status: string; detail: string | null; last_error: string | null; updated_at: number | null }
  | { type: "chat.accounts.changed" }
  | { type: string };

/** Seconds-since-epoch of a runtime blob (handles ISO strings and float seconds). */
function stamp(value: string | number | null | undefined): number {
  if (value == null) return 0;
  if (typeof value === "number") return value;
  const t = Date.parse(value);
  return Number.isNaN(t) ? 0 : t / 1000;
}

/**
 * A REST response can be older than a status event that arrived over the socket
 * while the request was in flight. Keep whichever runtime is newer per id.
 */
function newest<
  T extends { id: number; status: string; last_error: string | null; runtime: { updated_at: string | number | null } | null },
>(
  incoming: T[],
  current: T[],
): T[] {
  const byId = new Map(current.map((c) => [c.id, c]));
  return incoming.map((item) => {
    const local = byId.get(item.id);
    if (!local?.runtime) return item;
    if (!item.runtime || stamp(local.runtime.updated_at) > stamp(item.runtime.updated_at)) {
      return { ...item, status: local.status, runtime: local.runtime, last_error: local.last_error };
    }
    return item;
  });
}

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
  const [state, setState] = useState<EventsState>({ destinations: [], relay: null, accounts: [], chat: [], conn: "connecting", error: null });
  const wsRef = useRef<WebSocket | null>(null);
  const attemptRef = useRef(0);
  const stoppedRef = useRef(false);
  // status events for accounts the list does not contain yet (created a moment ago)
  const pendingAccountRef = useRef(new Map<number, Pick<ChatAccount, "status" | "last_error" | "runtime">>());

  const applyPending = useCallback((accounts: ChatAccount[]): ChatAccount[] => {
    const pending = pendingAccountRef.current;
    if (pending.size === 0) return accounts;
    return accounts.map((a) => {
      const p = pending.get(a.id);
      if (!p) return a;
      pending.delete(a.id);
      if (a.runtime && p.runtime && stamp(a.runtime.updated_at) >= stamp(p.runtime.updated_at)) return a;
      return { ...a, status: a.enabled ? p.status : "disabled", last_error: p.last_error, runtime: p.runtime };
    });
  }, []);

  const refresh = useCallback(async () => {
    // (applyPending is stable; declared above)
    try {
      const [destinations, relay, accounts] = await Promise.all([destApi.list(), relayApi.state(), chatApi.accounts()]);
      setState((s) => ({
        ...s,
        destinations: newest(destinations, s.destinations),
        relay,
        accounts: applyPending(newest(accounts, s.accounts)),
        error: null,
      }));
    } catch (err) {
      setState((s) => ({ ...s, error: err instanceof Error ? err.message : String(err) }));
    }
  }, []);

  const refreshAccounts = useCallback(async () => {
    try {
      const accounts = await chatApi.accounts();
      setState((s) => ({ ...s, accounts: applyPending(newest(accounts, s.accounts)) }));
    } catch {
      /* keep previous */
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
          case "chat.accounts.changed":
            void refreshAccounts();
            break;
          case "chat.account.status": {
            const ae = evt as Extract<AnyEvent, { type: "chat.account.status" }>;
            setState((s) => {
              if (!s.accounts.some((a) => a.id === ae.id)) {
                pendingAccountRef.current.set(ae.id, {
                  status: ae.status,
                  last_error: ae.last_error,
                  runtime: { status: ae.status, detail: ae.detail, last_error: ae.last_error, updated_at: ae.updated_at ?? Date.now() / 1000 },
                });
                return s;
              }
              return {
              ...s,
              accounts: s.accounts.map((a) =>
                a.id === ae.id
                  ? {
                      ...a,
                      status: a.enabled ? ae.status : "disabled",
                      last_error: ae.last_error,
                      runtime: { status: ae.status, detail: ae.detail, last_error: ae.last_error, updated_at: ae.updated_at ?? Date.now() / 1000 },
                    }
                  : a,
              ),
              };
            });
            break;
          }
          case "chat.message": {
            const { type: _t, ...message } = evt as Extract<AnyEvent, { type: "chat.message" }>;
            setState((s) => {
              if (s.chat.some((m) => m.id === message.id)) return s;
              const chat = [...s.chat, message as ChatMessage];
              return { ...s, chat: chat.length > MAX_LIVE_CHAT ? chat.slice(chat.length - MAX_LIVE_CHAT) : chat };
            });
            break;
          }
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
  }, [enabled, refresh, refreshAccounts]);


  return { ...state, refresh, refreshAccounts };
}
