# Things only the owner can do

Checked off items move to the bottom. Claude keeps working on everything that is
not blocked by these.

## Now (Phase 0/1)

- [ ] **DNS**: point a hostname (e.g. `stream.example.com`) at the VM's public IP
      and set `OMNI_DOMAIN` in `.env`. Without it Caddy uses a self-signed cert.
- [ ] **OCI security list / NSG** for the VM's subnet, ingress:
      `80/tcp`, `443/tcp`, `443/udp` (HTTP/3) from `0.0.0.0/0`;
      `1935/tcp` (RTMP) and `8890/udp` (SRT) ideally restricted to the IPs you
      stream from. See `deploy/README.md` for the host firewall step too (Oracle
      Ubuntu images ship with iptables rules that drop everything but 22).
- [ ] Run `python3 scripts/init_env.py --domain <hostname>` on the VM and store the
      printed admin password + stream key in your password manager.

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
