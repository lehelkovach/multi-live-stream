import { useEffect, useState } from "react";
import { ApiError, auth } from "./api";
import { Dashboard } from "./pages/Dashboard";
import { LoginPage } from "./pages/LoginPage";

type Session = { kind: "checking" } | { kind: "anonymous" } | { kind: "user"; username: string };

export function App() {
  const [session, setSession] = useState<Session>({ kind: "checking" });

  useEffect(() => {
    let cancelled = false;
    auth
      .me()
      .then((u) => !cancelled && setSession({ kind: "user", username: u.username }))
      .catch((err: unknown) => {
        if (cancelled) return;
        setSession({ kind: "anonymous" });
        if (!(err instanceof ApiError && err.status === 401)) console.warn("session check failed", err);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (session.kind === "checking") return <main className="shell center muted">…</main>;
  if (session.kind === "anonymous") return <LoginPage onLogin={(username) => setSession({ kind: "user", username })} />;
  return <Dashboard username={session.username} onLogout={() => setSession({ kind: "anonymous" })} />;
}
