import { useCallback, useState } from "react";
import { ApiError, auth, destinations as destApi, relay as relayApi } from "../api";
import { DestinationCard } from "../components/DestinationCard";
import { DestinationForm } from "../components/DestinationForm";
import { IngestPanel } from "../components/IngestPanel";
import { useEvents } from "../hooks/useEvents";
import type { Destination, DestinationInput } from "../types";

export function Dashboard({ username, onLogout }: { username: string; onLogout: () => void }) {
  const { destinations, relay, conn, error, refresh } = useEvents(true);
  const [editing, setEditing] = useState<Destination | null | "new">(null);
  const [busyIds, setBusyIds] = useState<Set<number>>(new Set());
  const [relayBusy, setRelayBusy] = useState(false);
  const [toast, setToast] = useState<string | null>(null);

  const flash = useCallback((msg: string) => {
    setToast(msg);
    window.setTimeout(() => setToast(null), 2500);
  }, []);

  const withBusy = async (id: number, fn: () => Promise<void>) => {
    setBusyIds((s) => new Set(s).add(id));
    try {
      await fn();
    } catch (err) {
      flash(err instanceof ApiError ? err.message : "request failed");
    } finally {
      setBusyIds((s) => {
        const n = new Set(s);
        n.delete(id);
        return n;
      });
    }
  };

  const onToggle = (dest: Destination, enabled: boolean) =>
    void withBusy(dest.id, async () => {
      await destApi.update(dest.id, { enabled });
      await refresh();
    });

  const onDelete = (dest: Destination) => {
    if (!window.confirm(`Delete "${dest.name}"?`)) return;
    void withBusy(dest.id, async () => {
      await destApi.remove(dest.id);
      await refresh();
    });
  };

  const onSave = async (input: DestinationInput, id: number | null) => {
    if (id === null) await destApi.create(input);
    else await destApi.update(id, input);
    setEditing(null);
    await refresh();
    flash(id === null ? "destination added" : "saved");
  };

  const onToggleRelay = async (enabled: boolean) => {
    setRelayBusy(true);
    try {
      await (enabled ? relayApi.start() : relayApi.stop());
      await refresh();
    } catch (err) {
      flash(err instanceof ApiError ? err.message : "request failed");
    } finally {
      setRelayBusy(false);
    }
  };

  const logout = async () => {
    try {
      await auth.logout();
    } finally {
      onLogout();
    }
  };

  return (
    <main className="shell">
      <header className="topbar">
        <span className="brand">OmniStream</span>
        <span className="topbar-right">
          <span className={`badge conn-${conn}`} data-testid="ws-status" data-state={conn} title="live updates">
            {conn === "connected" ? "online" : conn === "connecting" ? "connecting" : "offline"}
          </span>
          <button type="button" className="btn ghost" onClick={() => void logout()} data-testid="logout" title={username}>
            Sign out
          </button>
        </span>
      </header>

      <IngestPanel relay={relay} busy={relayBusy} onToggleRelay={(e) => void onToggleRelay(e)} />

      <section className="list">
        <div className="list-head">
          <h2>Destinations</h2>
          <button type="button" className="btn primary" onClick={() => setEditing("new")} data-testid="add-destination">
            + Add
          </button>
        </div>
        {error && <p className="error small">{error}</p>}
        {destinations.length === 0 ? (
          <div className="card empty muted">No destinations yet. Add YouTube, Twitch, Facebook, Kick, X or any RTMP/SRT target.</div>
        ) : (
          destinations.map((d) => (
            <DestinationCard key={d.id} dest={d} busy={busyIds.has(d.id)} onToggle={onToggle} onEdit={setEditing} onDelete={onDelete} />
          ))
        )}
      </section>

      {editing !== null && <DestinationForm initial={editing === "new" ? null : editing} onCancel={() => setEditing(null)} onSave={onSave} />}
      {toast && (
        <div className="toast" role="status" data-testid="toast">
          {toast}
        </div>
      )}
    </main>
  );
}
