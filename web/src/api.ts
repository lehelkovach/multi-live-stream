/** Thin fetch wrapper. Same-origin, cookie-based session, JSON in/out. */
import type { Destination, DestinationInput, IngestInfo, RelayState } from "./types";

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: { Accept: "application/json", ...(init.body ? { "Content-Type": "application/json" } : {}) },
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
      else if (Array.isArray(body.detail)) {
        const first = body.detail[0] as { msg?: string; loc?: unknown[] } | undefined;
        if (first?.msg) detail = `${(first.loc ?? []).slice(-1)[0] ?? "field"}: ${first.msg.replace(/^Value error, /, "")}`;
      }
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export interface Healthz {
  status: "ok";
  version: string;
}

export interface User {
  username: string;
}

const json = (body: unknown): RequestInit => ({ method: "POST", body: JSON.stringify(body) });

export const auth = {
  me: () => api<User>("/api/v1/auth/me"),
  login: (username: string, password: string) => api<User>("/api/v1/auth/login", json({ username, password })),
  logout: () => api<{ ok: boolean }>("/api/v1/auth/logout", { method: "POST" }),
};

export const destinations = {
  list: () => api<Destination[]>("/api/v1/destinations"),
  create: (input: DestinationInput) => api<Destination>("/api/v1/destinations", json(input)),
  update: (id: number, patch: Partial<DestinationInput>) =>
    api<Destination>(`/api/v1/destinations/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  remove: (id: number) => api<void>(`/api/v1/destinations/${id}`, { method: "DELETE" }),
};

export const relay = {
  state: () => api<RelayState>("/api/v1/relay"),
  start: () => api<{ ok: boolean }>("/api/v1/relay/start", { method: "POST" }),
  stop: () => api<{ ok: boolean }>("/api/v1/relay/stop", { method: "POST" }),
  ingest: () => api<IngestInfo>("/api/v1/relay/ingest"),
};
