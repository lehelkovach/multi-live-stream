import type { Status } from "../types";

const LABELS: Record<Status, string> = {
  disabled: "disabled",
  idle: "idle",
  starting: "starting",
  live: "live",
  retrying: "retrying",
  error: "error",
};

export function StatusPill({ status, testId }: { status: Status | string; testId?: string }) {
  const known = (status in LABELS ? status : "idle") as Status;
  return (
    <span className={`pill pill-${known}`} data-testid={testId ?? "dest-status"} data-status={known}>
      {status === "live" && <span className="dot" aria-hidden="true" />}
      {LABELS[known] ?? status}
    </span>
  );
}
