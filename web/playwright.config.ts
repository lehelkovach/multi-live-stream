import { defineConfig, devices } from "@playwright/test";

// The smoke test runs against tests/e2e/serve.py: API + built dashboard + the real
// relay supervisor with a fake ingest (no Docker, no ffmpeg). `make test-e2e`.
const port = Number(process.env.E2E_PORT ?? 8099);
const python = process.env.E2E_PYTHON ?? "../.venv/bin/python";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    trace: "retain-on-failure",
    ...devices["Pixel 7"], // mobile-first: the owner uses a phone
  },
  webServer: {
    command: `${python} ../tests/e2e/serve.py --port ${port}`,
    url: `http://127.0.0.1:${port}/healthz`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    stdout: "pipe",
    stderr: "pipe",
  },
});
