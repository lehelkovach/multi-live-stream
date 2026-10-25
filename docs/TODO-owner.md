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

## Later phases

- [ ] Phase 3/4 OAuth apps: Twitch (client id/secret), YouTube Data API (OAuth
      client, quota), Facebook app (pages_read_engagement etc.), Kick credentials.
      Details will be listed in `docs/platforms.md` as each adapter lands.
- [ ] Phase 5: decide which mobile app(s) to target first; accept the
      terms-of-service risk noted in `docs/mobile-spike.md` once written.
