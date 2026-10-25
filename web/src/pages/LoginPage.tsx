import { useState, type FormEvent } from "react";
import { ApiError, auth } from "../api";

export function LoginPage({ onLogin }: { onLogin: (username: string) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const user = await auth.login(username.trim(), password);
      onLogin(user.username);
    } catch (err) {
      setError(err instanceof ApiError ? (err.status === 429 ? "too many attempts, wait a few minutes" : "invalid credentials") : "network error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="shell login">
      <form className="card" onSubmit={submit}>
        <h1 className="brand">OmniStream</h1>
        <label>
          Username
          <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" autoCapitalize="off" required autoFocus data-testid="login-username" />
        </label>
        <label>
          Password
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required data-testid="login-password" />
        </label>
        {error && (
          <p className="error" role="alert" data-testid="login-error">
            {error}
          </p>
        )}
        <button type="submit" className="btn primary wide" disabled={busy} data-testid="login-submit">
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
