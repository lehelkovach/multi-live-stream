# Things only the owner can do

Checked off items move to the bottom. Claude keeps working on everything that is
not blocked by these.

## Now

- [ ] **GitHub Actions never runs**: every CI run is refused before a runner
      starts (no logs, ~3 s). That is an account-level block, usually a billing
      problem or a spending limit of $0 on a private repo, or Actions disabled.
      Check GitHub → Settings → Billing and plans, and the repo's Settings →
      Actions. Until then CI gives no signal; tests are run locally.
- [ ] **Admin password**: `deploy/oci/provision.py up` prints the generated one
      once (and writes it with `--password-file`). Store it in your password
      manager; change it on the VM with `make hash-password`, paste the quoted
      line into `/opt/omnistream/.env`, then `docker compose up -d api`.
- [ ] **Restrict ingest** (optional): RTMP/SRT are open to the internet and
      protected only by the stream key. To lock them to your own IP, re-run
      `provision.py up --ingest-cidr <your-ip>/32` (it updates the rules in place).
- [ ] **Hostname** (decide before registering OAuth apps): the automated deploy
      uses `<ip-with-dashes>.sslip.io`. The public IP survives reboots and
      stop/start but changes if the VM is recreated, and the OAuth redirect URIs
      below would then break. For a stable name, point your own DNS record at the
      VM and set `OMNI_DOMAIN` in `/opt/omnistream/.env` (or pass `--domain` to
      `provision.py up` for a new VM).

Manual (non-scripted) installs also need the OCI security list, the host
firewall and `scripts/init_env.py`; see `deploy/README.md`.

## Phase 3 - chat (OAuth apps; each is ~5 minutes in the platform console)

Redirect URI for every app: `https://<OMNI_DOMAIN>/api/v1/chat/oauth/<platform>/callback`.
Put the ids/secrets in `.env` (`OMNI_*_CLIENT_ID` / `_SECRET`) and `make up`; the
Accounts tab then shows a **Connect** button per platform.

- [ ] **Twitch**: https://dev.twitch.tv/console/apps → Register, category "Chat Bot",
      OAuth redirect `.../oauth/twitch/callback`, client type *Confidential*.
      Scopes requested: `user:read:chat user:write:chat`.
- [ ] **YouTube**: Google Cloud project → enable *YouTube Data API v3* → OAuth consent
      screen (External, add yourself as test user) → Credentials → OAuth client
      (Web application) with redirect `.../oauth/youtube/callback`. Consider a quota
      increase request if you stream more than ~2 h/day (see docs/platforms.md).
- [ ] **Kick**: https://kick.com/settings/developer → create app, redirect
      `.../oauth/kick/callback`, and set the **webhook URL** to
      `https://<OMNI_DOMAIN>/api/v1/webhooks/kick` (enable webhooks).
- [ ] **Facebook**: https://developers.facebook.com → app with *Facebook Login* →
      Valid OAuth Redirect URI `.../oauth/facebook/callback`. While the app is in
      development mode only admins/testers can connect, which is fine for a single
      owner; app review is needed for anything else. Alternative: paste a Page
      access token via "Token" on the Accounts tab.

## Later phases
- [ ] Phase 5: decide which mobile app(s) to target first; accept the
      terms-of-service risk noted in `docs/mobile-spike.md` once written.
