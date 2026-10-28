import { expect, test, type Page } from "@playwright/test";

const USER = "admin";
const PASSWORD = "e2e-password-123";

async function login(page: Page) {
  await page.goto("/");
  await expect(page.getByTestId("login-submit")).toBeVisible();
  await page.getByTestId("login-username").fill(USER);
  await page.getByTestId("login-password").fill(PASSWORD);
  await page.getByTestId("login-submit").click();
  await expect(page.getByRole("heading", { name: "Destinations" })).toBeVisible();
}

test.describe.configure({ mode: "serial" });

test("rejects a wrong password", async ({ page }) => {
  await page.goto("/");
  await page.getByTestId("login-username").fill(USER);
  await page.getByTestId("login-password").fill("nope");
  await page.getByTestId("login-submit").click();
  await expect(page.getByTestId("login-error")).toHaveText(/invalid credentials/);
});

test("login → add destination → toggle → live status over websocket", async ({ page, request }) => {
  await request.post("/__test/ingest/off");
  await login(page);
  await expect(page.getByTestId("ws-status")).toHaveAttribute("data-state", "connected");
  await expect(page.getByTestId("ingest-status")).toHaveAttribute("data-live", "false");

  // add
  await page.getByTestId("add-destination").click();
  await page.getByTestId("form-name").fill("Smoke YT");
  await page.getByTestId("form-platform").selectOption("youtube");
  await expect(page.getByTestId("form-url")).toHaveValue("rtmp://a.rtmp.youtube.com/live2");
  await page.getByTestId("form-key").fill("abcd-1234-efgh-5678");
  await page.getByTestId("form-submit").click();
  const card = page.getByTestId("dest-card").filter({ hasText: "Smoke YT" });
  await expect(card).toBeVisible();
  await expect(card.getByTestId("dest-status")).toHaveAttribute("data-status", "idle");
  await expect(card).toContainText("key ••••678");

  // validation error surfaces
  await page.getByTestId("add-destination").click();
  await page.getByTestId("form-name").fill("Bad");
  await page.getByTestId("form-platform").selectOption("custom");
  await page.getByTestId("form-url").fill("http://not-rtmp.example");
  await page.getByTestId("form-submit").click();
  await expect(page.getByTestId("form-error")).toContainText(/rtmp_url/);
  await page.keyboard.press("Escape");

  // toggle off / on -> status follows (pushed by the relay through the websocket)
  await card.getByTestId("dest-toggle").click();
  await expect(card.getByTestId("dest-status")).toHaveAttribute("data-status", "disabled");
  await card.getByTestId("dest-toggle").click();
  await expect(card.getByTestId("dest-status")).toHaveAttribute("data-status", "idle");

  // ingest goes live -> the relay starts the output -> card turns live without any click
  await request.post("/__test/ingest/on");
  await expect(page.getByTestId("ingest-status")).toHaveAttribute("data-live", "true");
  await expect(card.getByTestId("dest-status")).toHaveAttribute("data-status", "live", { timeout: 15_000 });
  await expect(card.getByTestId("dest-bitrate")).toContainText("Mbps");
  await expect(page.getByTestId("relay-summary")).toContainText("1/1 live");

  // stop all / start all
  await page.getByTestId("relay-toggle").click();
  await expect(card.getByTestId("dest-status")).toHaveAttribute("data-status", "idle");
  await expect(card).toContainText("relay stopped");
  await page.getByTestId("relay-toggle").click();
  await expect(card.getByTestId("dest-status")).toHaveAttribute("data-status", "live", { timeout: 15_000 });

  // ingest ends -> back to idle
  await request.post("/__test/ingest/off");
  await expect(card.getByTestId("dest-status")).toHaveAttribute("data-status", "idle", { timeout: 15_000 });
  await expect(page.getByTestId("ingest-status")).toHaveAttribute("data-live", "false");

  // edit keeps the key, renames
  await card.getByTestId("dest-edit").click();
  await expect(page.getByTestId("form-key")).toHaveAttribute("placeholder", /unchanged/);
  await page.getByTestId("form-name").fill("Smoke YT renamed");
  await page.getByTestId("form-submit").click();
  const renamed = page.getByTestId("dest-card").filter({ hasText: "Smoke YT renamed" });
  await expect(renamed).toBeVisible();
  await expect(renamed).toContainText("key ••••678");

  // OBS settings panel
  await page.getByTestId("obs-toggle").click();
  await expect(page.getByTestId("obs-settings")).toContainText("rtmp://localhost:1935/live");

  // session survives a reload, then delete + sign out
  await page.reload();
  await expect(page.getByRole("heading", { name: "Destinations" })).toBeVisible();
  page.once("dialog", (d) => d.accept());
  await renamed.getByTestId("dest-delete").click();
  await expect(renamed).toHaveCount(0);
  await page.getByTestId("logout").click();
  await expect(page.getByTestId("login-submit")).toBeVisible();
});

test("chat: add a fake account → messages stream into the unified pane", async ({ page }) => {
  const tag = Math.random().toString(36).slice(2, 8); // keeps repeated runs on one server independent
  const name = `Demo chat ${tag}`;
  await login(page);
  await page.getByTestId("tab-accounts").click();
  await page.getByTestId("manual-fake").click();
  await page.getByTestId("manual-name").fill(name);
  await page.getByTestId("manual-submit").click();
  const account = page.getByTestId("chat-account").filter({ hasText: name });
  await expect(account).toBeVisible();
  await expect(account.getByTestId("account-status")).toHaveAttribute("data-status", "connected", { timeout: 15_000 });

  await page.getByTestId("tab-chat").click();
  await expect(page.getByTestId("chat-message").first()).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId("chat-message").nth(2)).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId("chat-message").first()).toContainText("fake platform");
  await expect(page.getByTestId("chat-summary")).toContainText("1/1 connected");

  // compose: broadcast to all -> delivery tick + the platform echo shows up as our own message
  await page.getByTestId("compose-input").fill(`hello from the dashboard ${tag}`);
  await page.getByTestId("compose-send").click();
  const outgoing = page.getByTestId("outgoing-row").filter({ hasText: `hello from the dashboard ${tag}` });
  await expect(outgoing).toBeVisible();
  await expect(outgoing.getByTestId("delivery")).toHaveAttribute("data-status", "sent", { timeout: 15_000 });
  await expect(outgoing).toHaveAttribute("data-status", "sent", { timeout: 15_000 });
  await expect(page.locator(".msg.own").filter({ hasText: `hello from the dashboard ${tag}` })).toBeVisible({ timeout: 15_000 });

  // a failing send is reported per target
  await page.getByTestId("compose-input").fill(`[fail] this one bounces ${tag}`);
  await page.getByTestId("compose-send").click();
  const failed = page.getByTestId("outgoing-row").filter({ hasText: `this one bounces ${tag}` });
  await expect(failed.getByTestId("delivery")).toHaveAttribute("data-status", "failed", { timeout: 15_000 });
  await expect(failed).toHaveAttribute("data-status", "failed", { timeout: 15_000 });

  // tap-to-reply pre-selects the platform
  await page.getByTestId("chat-message").first().click();
  await expect(page.getByTestId("replying")).toContainText("replying to");
  await page.getByTestId("compose-input").fill(`thanks ${tag}`);
  await page.getByTestId("compose-send").click();
  await expect(page.getByTestId("outgoing-row").filter({ hasText: `thanks ${tag}` }).getByTestId("delivery")).toHaveAttribute("data-status", "sent", { timeout: 15_000 });

  // messages survive a reload (history) and keep flowing
  const before = await page.getByTestId("chat-message").count();
  await page.reload();
  await page.getByTestId("tab-chat").click();
  await expect(page.getByTestId("chat-message").first()).toBeVisible();
  await expect.poll(async () => page.getByTestId("chat-message").count(), { timeout: 15_000 }).toBeGreaterThan(before);

  // disabling the account stops it without touching the history
  await page.getByTestId("tab-accounts").click();
  await account.getByTestId("account-toggle").click();
  await expect(account.getByTestId("account-status")).toHaveAttribute("data-status", "disabled", { timeout: 15_000 });
  page.once("dialog", (d) => d.accept());
  await account.getByRole("button", { name: "Disconnect" }).click();
  await expect(account).toHaveCount(0);
});
