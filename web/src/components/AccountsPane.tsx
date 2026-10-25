import { useEffect, useState, type FormEvent } from "react";
import { ApiError, chat as chatApi } from "../api";
import type { ChatAccount, ChatPlatformInfo } from "../types";
import { StatusPill } from "./StatusPill";
import { Toggle } from "./Toggle";

const PLATFORM_LETTER: Record<string, string> = { twitch: "TW", youtube: "YT", kick: "KI", facebook: "FB", fake: "FK" };
const letters = (platform: string) => PLATFORM_LETTER[platform] ?? platform.slice(0, 2).toUpperCase();

interface Props {
  accounts: ChatAccount[];
  onChanged: () => Promise<void>;
  flash: (msg: string) => void;
}

export function AccountsPane({ accounts, onChanged, flash }: Props) {
  const [platforms, setPlatforms] = useState<ChatPlatformInfo[]>([]);
  const [manual, setManual] = useState<string | null>(null);
  const [busy, setBusy] = useState<number | null>(null);

  useEffect(() => {
    chatApi.platforms().then(setPlatforms).catch(() => setPlatforms([]));
  }, []);

  const connect = async (platform: string) => {
    try {
      const { url } = await chatApi.oauthStart(platform);
      window.location.assign(url);
    } catch (err) {
      flash(err instanceof ApiError ? err.message : "could not start sign-in");
    }
  };

  const toggle = async (acc: ChatAccount, enabled: boolean) => {
    setBusy(acc.id);
    try {
      await chatApi.updateAccount(acc.id, { enabled });
      await onChanged();
    } catch (err) {
      flash(err instanceof ApiError ? err.message : "request failed");
    } finally {
      setBusy(null);
    }
  };

  const remove = async (acc: ChatAccount) => {
    if (!window.confirm(`Disconnect ${acc.display_name}?`)) return;
    setBusy(acc.id);
    try {
      await chatApi.deleteAccount(acc.id);
      await onChanged();
    } finally {
      setBusy(null);
    }
  };

  const pickPage = async (acc: ChatAccount, pageId: string) => {
    const page = acc.pages.find((p) => p.id === pageId);
    await chatApi.updateAccount(acc.id, { channel_ref: pageId, display_name: page?.name ?? acc.display_name });
    await onChanged();
  };

  return (
    <section className="accounts">
      <div className="card">
        <h2>Connect a platform</h2>
        <p className="muted small">OAuth keeps tokens on this server, encrypted. Platforms without an OAuth app configured can still be added with a pasted token.</p>
        <div className="connect-grid">
          {platforms.map((p) => (
            <div key={p.platform} className="connect-row">
              <span className={`avatar avatar-${p.platform}`}>{letters(p.platform)}</span>
              <span className="connect-name">{p.label}</span>
              <span className="spacer" />
              {p.oauth_configured && (
                <button type="button" className="btn primary" onClick={() => void connect(p.platform)} data-testid={`connect-${p.platform}`}>
                  Connect
                </button>
              )}
              <button type="button" className="btn ghost" onClick={() => setManual(p.platform)} data-testid={`manual-${p.platform}`}>
                Token
              </button>
            </div>
          ))}
        </div>
      </div>

      <h2 className="section-title">Connected accounts</h2>
      {accounts.length === 0 && <div className="card empty muted">Nothing connected yet.</div>}
      {accounts.map((acc) => (
        <article key={acc.id} className="card account" data-testid="chat-account">
          <div className="dest-head">
            <span className={`avatar avatar-${acc.platform}`}>{letters(acc.platform)}</span>
            <div className="dest-title">
              <div className="dest-name">{acc.display_name || acc.platform}</div>
              <div className="muted small">
                {acc.platform}
                {acc.channel_ref ? <> · {acc.channel_ref}</> : null}
              </div>
            </div>
            <StatusPill status={acc.status} testId="account-status" />
          </div>
          <div className="dest-stats">
            {acc.last_error && acc.status !== "connected" ? (
              <span className="error">{acc.last_error}</span>
            ) : (
              <span className="muted">{acc.runtime?.detail ?? (acc.status === "connected" ? "receiving messages" : "")}</span>
            )}
          </div>
          {acc.platform === "facebook" && acc.pages.length > 1 && (
            <label>
              Page
              <select value={acc.channel_ref} onChange={(e) => void pickPage(acc, e.target.value)}>
                <option value="">choose a page…</option>
                {acc.pages.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>
          )}
          <div className="dest-actions">
            <Toggle checked={acc.enabled} disabled={busy === acc.id} label={`Enable ${acc.display_name}`} testId="account-toggle" onChange={(v) => void toggle(acc, v)} />
            <span className="spacer" />
            <button type="button" className="btn ghost danger" onClick={() => void remove(acc)}>
              Disconnect
            </button>
          </div>
        </article>
      ))}

      {manual && (
        <ManualTokenForm
          platform={manual}
          hint={platforms.find((p) => p.platform === manual)?.manual_hint ?? ""}
          onCancel={() => setManual(null)}
          onSaved={async () => {
            setManual(null);
            await onChanged();
            flash("account added");
          }}
        />
      )}
    </section>
  );
}

function ManualTokenForm({ platform, hint, onCancel, onSaved }: { platform: string; hint: string; onCancel: () => void; onSaved: () => Promise<void> }) {
  const [name, setName] = useState("");
  const [channelRef, setChannelRef] = useState("");
  const [token, setToken] = useState("");
  const [refresh, setRefresh] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      await chatApi.createAccount({ platform, display_name: name, channel_ref: channelRef, access_token: token, refresh_token: refresh, enabled: true, extra: {} });
      await onSaved();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "could not save");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="sheet-backdrop" onClick={onCancel} role="presentation">
      <form className="sheet" onClick={(e) => e.stopPropagation()} onSubmit={submit} data-testid="manual-form">
        <h2>Add {platform} with a token</h2>
        <p className="muted small">{hint}</p>
        <label>
          Name
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder={platform} data-testid="manual-name" />
        </label>
        <label>
          Channel / page reference
          <input value={channelRef} onChange={(e) => setChannelRef(e.target.value)} autoCapitalize="off" data-testid="manual-ref" />
        </label>
        {platform !== "fake" && (
          <>
            <label>
              Access token
              <input type="password" value={token} onChange={(e) => setToken(e.target.value)} autoCapitalize="off" data-testid="manual-token" />
            </label>
            <label>
              Refresh token (optional)
              <input type="password" value={refresh} onChange={(e) => setRefresh(e.target.value)} autoCapitalize="off" />
            </label>
          </>
        )}
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <div className="sheet-actions">
          <button type="button" className="btn ghost" onClick={onCancel}>
            Cancel
          </button>
          <button type="submit" className="btn primary" disabled={saving} data-testid="manual-submit">
            {saving ? "Saving…" : "Add"}
          </button>
        </div>
      </form>
    </div>
  );
}
