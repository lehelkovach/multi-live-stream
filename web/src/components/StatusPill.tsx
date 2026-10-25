/** Status badge shared by destinations (relay) and chat accounts. */

const LABELS: Record<string, string> = {
  // relay destinations
  disabled: "disabled",
  idle: "idle",
  starting: "starting",
  live: "live",
  retrying: "retrying",
  error: "error",
  // chat accounts
  connecting: "connecting",
  connected: "connected",
  waiting: "waiting",
  auth_error: "needs login",
};

const PULSING = new Set(["live", "connected"]);

export function StatusPill({ status, testId }: { status: string; testId?: string }) {
  const label = LABELS[status] ?? status.replace(/_/g, " ");
  return (
    <span className={`pill pill-${status}`} data-testid={testId ?? "dest-status"} data-status={status}>
      {PULSING.has(status) && <span className="dot" aria-hidden="true" />}
      {label}
    </span>
  );
}
