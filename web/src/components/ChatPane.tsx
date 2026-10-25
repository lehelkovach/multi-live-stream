import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { ApiError, chat as chatApi } from "../api";
import type { ChatAccount, ChatMessage, Outgoing } from "../types";

const PLATFORM_SHORT: Record<string, string> = { twitch: "TW", youtube: "YT", kick: "KI", facebook: "FB", fake: "FK" };

interface Props {
  live: ChatMessage[]; // pushed over the websocket (newest last)
  accounts: ChatAccount[];
  outgoing: Outgoing[];
  onSent: (o: Outgoing) => void;
}

interface ReplyTarget {
  id: string;
  author: string;
  account_id: number | null;
  platform: string;
}

export function ChatPane({ live, accounts, outgoing, onSent }: Props) {
  const [history, setHistory] = useState<ChatMessage[]>([]);
  const [filter, setFilter] = useState<string>("all");
  const [stick, setStick] = useState(true);
  const [text, setText] = useState("");
  const [targets, setTargets] = useState<number[] | "all">("all");
  const [reply, setReply] = useState<ReplyTarget | null>(null);
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const sendable = accounts.filter((a) => a.enabled);

  const toggleTarget = (id: number) => {
    setTargets((t) => {
      if (t === "all") return sendable.filter((a) => a.id !== id).map((a) => a.id);
      const next = t.includes(id) ? t.filter((x) => x !== id) : [...t, id];
      return next.length === sendable.length ? "all" : next;
    });
  };

  const startReply = (m: ChatMessage) => {
    setReply({ id: m.id, author: m.author, account_id: m.account_id, platform: m.platform });
    if (m.account_id != null) setTargets([m.account_id]);
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const body = text.trim();
    if (!body || sending) return;
    if (targets !== "all" && targets.length === 0) {
      setSendError("pick at least one target");
      return;
    }
    setSending(true);
    setSendError(null);
    try {
      const out = await chatApi.send(body, targets, reply?.id ?? null);
      onSent(out);
      setText("");
      setReply(null);
      setTargets("all");
    } catch (err) {
      setSendError(err instanceof ApiError ? err.message : "send failed");
    } finally {
      setSending(false);
    }
  };

  useEffect(() => {
    chatApi
      .messages({ limit: 200 })
      .then(setHistory)
      .catch(() => setHistory([]));
  }, []);

  type Row = { kind: "msg"; m: ChatMessage; ts: string } | { kind: "out"; o: Outgoing; ts: string };
  const merged = useMemo<Row[]>(() => {
    const seen = new Set<string>();
    const rows: Row[] = [];
    for (const m of [...history, ...live]) {
      if (seen.has(m.id)) continue;
      seen.add(m.id);
      if (filter === "all" || m.platform === filter) rows.push({ kind: "msg", m, ts: m.timestamp });
    }
    for (const o of outgoing) rows.push({ kind: "out", o, ts: o.created_at });
    rows.sort((a, b) => a.ts.localeCompare(b.ts));
    return rows;
  }, [history, live, outgoing, filter]);

  useEffect(() => {
    if (stick && listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight;
  }, [merged, stick]);

  const onScroll = () => {
    const el = listRef.current;
    if (!el) return;
    setStick(el.scrollHeight - el.scrollTop - el.clientHeight < 40);
  };

  const platforms = Array.from(new Set(accounts.map((a) => a.platform)));
  const connected = accounts.filter((a) => a.status === "connected").length;

  return (
    <section className="chat">
      <div className="chat-head">
        <div className="chips">
          <button type="button" className={`chip ${filter === "all" ? "on" : ""}`} onClick={() => setFilter("all")}>
            All
          </button>
          {platforms.map((p) => (
            <button key={p} type="button" className={`chip chip-${p} ${filter === p ? "on" : ""}`} onClick={() => setFilter(p)}>
              {PLATFORM_SHORT[p] ?? p}
            </button>
          ))}
        </div>
        <span className="muted small" data-testid="chat-summary">
          {connected}/{accounts.length} connected
        </span>
      </div>
      <div className="chat-list" ref={listRef} onScroll={onScroll} data-testid="chat-list">
        {merged.length === 0 ? (
          <div className="card empty muted">
            {accounts.length === 0 ? "Connect a chat account in the Accounts tab to see messages here." : "No messages yet."}
          </div>
        ) : (
          merged.map((row) =>
            row.kind === "msg" ? (
              <MessageRow key={row.m.id} m={row.m} onReply={startReply} />
            ) : (
              <OutgoingRow key={`out-${row.o.id}`} o={row.o} />
            ),
          )
        )}
      </div>
      {!stick && (
        <button
          type="button"
          className="btn primary jump"
          onClick={() => {
            setStick(true);
            if (listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight;
          }}
        >
          Jump to latest
        </button>
      )}

      <form className="compose" onSubmit={submit} data-testid="compose">
        {sendable.length > 0 && (
          <div className="targets" data-testid="compose-targets">
            <button type="button" className={`chip ${targets === "all" ? "on" : ""}`} onClick={() => setTargets("all")} data-testid="target-all">
              All
            </button>
            {sendable.map((a) => {
              const on = targets === "all" || targets.includes(a.id);
              return (
                <button key={a.id} type="button" className={`chip chip-${a.platform} ${on ? "on" : ""}`} onClick={() => toggleTarget(a.id)} title={a.display_name} data-testid={`target-${a.id}`}>
                  {PLATFORM_SHORT[a.platform] ?? a.platform}
                </button>
              );
            })}
          </div>
        )}
        {reply && (
          <div className="replying muted small" data-testid="replying">
            replying to <b>{reply.author}</b> on {reply.platform}
            <button type="button" className="linkish" onClick={() => { setReply(null); setTargets("all"); }}>
              cancel
            </button>
          </div>
        )}
        <div className="compose-row">
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={sendable.length ? (targets === "all" ? "Message all platforms…" : "Message selected…") : "Connect an account to send"}
            disabled={sendable.length === 0}
            maxLength={500}
            enterKeyHint="send"
            autoCapitalize="sentences"
            data-testid="compose-input"
          />
          <button type="submit" className="btn primary" disabled={sending || !text.trim() || sendable.length === 0} data-testid="compose-send">
            Send
          </button>
        </div>
        {sendError && (
          <p className="error small" role="alert">
            {sendError}
          </p>
        )}
      </form>
    </section>
  );
}

const DELIVERY_GLYPH: Record<string, string> = { pending: "…", sent: "✓", failed: "✗", rate_limited: "⏳", offline: "○", unsupported: "–" };

function OutgoingRow({ o }: { o: Outgoing }) {
  const time = new Date(o.created_at);
  return (
    <div className={`msg outgoing out-${o.status}`} data-testid="outgoing-row" data-status={o.status}>
      <span className="tag tag-own" title="you">ME</span>
      <span className="msg-body">
        <span className="msg-text">{o.text}</span>
        <span className="deliveries">
          {o.deliveries.map((d) => (
            <span key={d.account_id} className={`delivery delivery-${d.status}`} title={d.error ? `${d.name}: ${d.error}` : d.name} data-testid="delivery" data-status={d.status}>
              {PLATFORM_SHORT[d.platform] ?? d.platform} {DELIVERY_GLYPH[d.status] ?? d.status}
            </span>
          ))}
        </span>
      </span>
      <span className="msg-time muted small">{time.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
    </div>
  );
}

function MessageRow({ m, onReply }: { m: ChatMessage; onReply: (m: ChatMessage) => void }) {
  const time = new Date(m.timestamp);
  return (
    <div
      className={`msg msg-${m.platform} ${m.is_own ? "own" : ""} ${m.kind === "superchat" ? "superchat" : ""}`}
      data-testid="chat-message"
      onClick={() => !m.is_own && onReply(m)}
      role={m.is_own ? undefined : "button"}
      title={m.is_own ? undefined : "tap to reply"}
    >
      <span className={`tag tag-${m.platform}`} title={m.platform}>
        {PLATFORM_SHORT[m.platform] ?? m.platform}
      </span>
      <span className="msg-body">
        <span className="msg-author">
          {m.author || "anonymous"}
          {m.badges.map((b) => (
            <span key={b} className={`mini-badge mini-${b}`} title={b}>
              {BADGE_GLYPH[b] ?? "•"}
            </span>
          ))}
        </span>{" "}
        <span className="msg-text">{m.text}</span>
      </span>
      <span className="msg-time muted small">
        {time.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
      </span>
    </div>
  );
}

const BADGE_GLYPH: Record<string, string> = { moderator: "⚔", owner: "★", broadcaster: "★", subscriber: "✦", member: "✦", vip: "◆", verified: "✓" };
