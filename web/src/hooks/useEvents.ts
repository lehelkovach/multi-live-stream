/**
 * Live dashboard state: REST for the initial/corrective loads, a WebSocket on
 * /ws/events for pushes from the relay and the API. Reconnects with backoff and
 * re-syncs from REST after every (re)connect and every 30 s as a safety net.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { chat as chatApi, destinations as destApi, relay as relayApi } from "../api";
import type { ChatAccount, ChatMessage, Delivery, Destination, Outgoing, RelayState, Runtime, Status } from "../types";

export type ConnState = "connecting" | "connected" | "offline";

interface EventsState {
  destinations: Destination[];
  relay: RelayState | null;
  accounts: ChatAccount[];
  chat: ChatMessage[]; // live messages received over the socket (bounded)
  outgoing: Outgoing[]; // recent sends with per-target delivery status (newest last)
  conn: ConnState;
  error: string | null;
  // Socket events that can arrive before the item they describe is in state (an
  // account created a moment ago, a send whose POST response is still in flight).
  // Kept in state, not refs, so every updater stays pure and order-independent.
  pendingAccounts: Record<number, AccountPatch>;
  pendingOutgoing: Record<number, OutgoingEvent[]>;
}

type AccountPatch = Pick<ChatAccount, "status" | "last_error" | "runtime">;
type OutgoingEvent = { id: number; account_id: number | null; done?: boolean } & Partial<Delivery>;

const MAX_LIVE_CHAT = 500;
const MAX_PENDING_OUTGOING = 50; // events for sends made from another device are never claimed

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
  | ({ type: "chat.outgoing"; id: number; account_id: number | null; done?: boolean } & Partial<Delivery>)
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

/** Merge account status events that arrived before the account list knew the id. */
function withPendingAccounts(
  accounts: ChatAccount[],
  pending: Record<number, AccountPatch>,
): { accounts: ChatAccount[]; pending: Record<number, AccountPatch> } {
  if (Object.keys(pending).length === 0) return { accounts, pending };
  const rest = { ...pending };
  const merged = accounts.map((a) => {
    const p = rest[a.id];
    if (!p) return a;
    delete rest[a.id];
    if (a.runtime && p.runtime && stamp(a.runtime.updated_at) >= stamp(p.runtime.updated_at)) return a;
    return { ...a, status: a.enabled ? p.status : "disabled", last_error: p.last_error, runtime: p.runtime };
  });
  return { accounts: merged, pending: rest };
}

function applyOutgoingEvent(o: Outgoing, oe: OutgoingEvent): Outgoing {
  if (oe.account_id == null) {
    return { ...o, status: oe.status ?? o.status, finished_at: oe.done ? new Date().toISOString() : o.finished_at };
  }
  const deliveries = o.deliveries.map((d) =>
    d.account_id === oe.account_id
      ? { ...d, status: oe.status ?? d.status, error: oe.error ?? null, message_id: oe.message_id ?? null, at: oe.at ?? null }
      : d,
  );
  return { ...o, deliveries };
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
  const [state, setState] = useState<EventsState>({
    destinations: [],
    relay: null,
    accounts: [],
    chat: [],
    outgoing: [],
    conn: "connecting",
    error: null,
    pendingAccounts: {},
    pendingOutgoing: {},
  });
  const wsRef = useRef<WebSocket | null>(null);
  const attemptRef = useRef(0);
  const stoppedRef = useRef(false);

  const refresh = useCallback(async () => {
    try {
      const [destinations, relay, accounts] = await Promise.all([destApi.list(), relayApi.state(), chatApi.accounts()]);
      setState((s) => {
        const merged = withPendingAccounts(newest(accounts, s.accounts), s.pendingAccounts);
        return {
          ...s,
          destinations: newest(destinations, s.destinations),
          relay,
          accounts: merged.accounts,
          pendingAccounts: merged.pending,
          error: null,
        };
      });
    } catch (err) {
      setState((s) => ({ ...s, error: err instanceof Error ? err.message : String(err) }));
    }
  }, []);

  const refreshAccounts = useCallback(async () => {
    try {
      const accounts = await chatApi.accounts();
      setState((s) => {
        const merged = withPendingAccounts(newest(accounts, s.accounts), s.pendingAccounts);
        return { ...s, accounts: merged.accounts, pendingAccounts: merged.pending };
      });
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
            const patch: AccountPatch = {
              status: ae.status,
              last_error: ae.last_error,
              runtime: { status: ae.status, detail: ae.detail, last_error: ae.last_error, updated_at: ae.updated_at ?? Date.now() / 1000 },
            };
            setState((s) => {
              if (!s.accounts.some((a) => a.id === ae.id)) {
                return { ...s, pendingAccounts: { ...s.pendingAccounts, [ae.id]: patch } };
              }
              return {
                ...s,
                accounts: s.accounts.map((a) =>
                  a.id === ae.id ? { ...a, ...patch, status: a.enabled ? patch.status : "disabled" } : a,
                ),
              };
            });
            break;
          }
          case "chat.outgoing": {
            const oe = evt as Extract<AnyEvent, { type: "chat.outgoing" }>;
            setState((s) => {
              if (!s.outgoing.some((o) => o.id === oe.id)) {
                // the chat service can finish before the browser has handled the POST response
                const pending = { ...s.pendingOutgoing, [oe.id]: [...(s.pendingOutgoing[oe.id] ?? []), oe] };
                const ids = Object.keys(pending).map(Number).sort((a, b) => a - b);
                for (const stale of ids.slice(0, Math.max(0, ids.length - MAX_PENDING_OUTGOING))) delete pending[stale];
                return { ...s, pendingOutgoing: pending };
              }
              return { ...s, outgoing: s.outgoing.map((o) => (o.id === oe.id ? applyOutgoingEvent(o, oe) : o)) };
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

  /** Register a send we just made; applies any of its events that arrived first. */
  const trackOutgoing = useCallback((incoming: Outgoing) => {
    setState((s) => {
      const { [incoming.id]: early = [], ...rest } = s.pendingOutgoing;
      const o = early.reduce(applyOutgoingEvent, incoming);
      return { ...s, outgoing: [...s.outgoing.filter((x) => x.id !== o.id), o].slice(-50), pendingOutgoing: rest };
    });
  }, []);

  const { pendingAccounts: _pa, pendingOutgoing: _po, ...visible } = state;
  return { ...visible, refresh, refreshAccounts, trackOutgoing };
}
