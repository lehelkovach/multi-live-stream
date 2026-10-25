import { useEffect, useState } from "react";
import { api, type Healthz } from "./api";

type State = { kind: "loading" } | { kind: "ok"; health: Healthz } | { kind: "error"; message: string };

export function App() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    api<Healthz>("/healthz")
      .then((health) => !cancelled && setState({ kind: "ok", health }))
      .catch((err: unknown) => {
        if (cancelled) return;
        setState({ kind: "error", message: err instanceof Error ? err.message : String(err) });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <main className="shell">
      <header className="topbar">
        <span className="brand">OmniStream</span>
        <HealthBadge state={state} />
      </header>
      <section className="card">
        <h1>Phase 0: skeleton</h1>
        <p>
          The stack is up. Dashboard features (login, destinations, live status, chat) arrive in
          the following phases.
        </p>
        {state.kind === "ok" && (
          <p className="muted">
            API version <code>{state.health.version}</code>
          </p>
        )}
        {state.kind === "error" && <p className="error">API unreachable: {state.message}</p>}
      </section>
    </main>
  );
}

function HealthBadge({ state }: { state: State }) {
  const label = state.kind === "loading" ? "checking" : state.kind === "ok" ? "api ok" : "api down";
  return (
    <span className={`badge badge-${state.kind}`} data-testid="health-badge">
      {label}
    </span>
  );
}
