import { useEffect, useMemo, useRef, useState } from "react";
import { chat as chatApi } from "../api";
import type { ChatAccount, ChatMessage } from "../types";

const PLATFORM_SHORT: Record<string, string> = { twitch: "TW", youtube: "YT", kick: "KI", facebook: "FB", fake: "FK" };

interface Props {
  live: ChatMessage[]; // pushed over the websocket (newest last)
  accounts: ChatAccount[];
}

export function ChatPane({ live, accounts }: Props) {
  const [history, setHistory] = useState<ChatMessage[]>([]);
  const [filter, setFilter] = useState<string>("all");
  const [stick, setStick] = useState(true);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    chatApi
      .messages({ limit: 200 })
      .then(setHistory)
      .catch(() => setHistory([]));
  }, []);

  const merged = useMemo(() => {
    const seen = new Set<string>();
    const out: ChatMessage[] = [];
    for (const m of [...history, ...live]) {
      if (seen.has(m.id)) continue;
      seen.add(m.id);
      out.push(m);
    }
    out.sort((a, b) => a.timestamp.localeCompare(b.timestamp));
    return filter === "all" ? out : out.filter((m) => m.platform === filter);
  }, [history, live, filter]);

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
          merged.map((m) => <MessageRow key={m.id} m={m} />)
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
    </section>
  );
}

function MessageRow({ m }: { m: ChatMessage }) {
  const time = new Date(m.timestamp);
  return (
    <div className={`msg msg-${m.platform} ${m.is_own ? "own" : ""} ${m.kind === "superchat" ? "superchat" : ""}`} data-testid="chat-message">
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
