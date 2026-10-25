# Deploying on OCI Ampere A1 (arm64)

## VM

* Shape `VM.Standard.A1.Flex` (4 OCPU / 24 GB is in the Always Free tier), Ubuntu
  22.04 or 24.04 **aarch64** image, boot volume ≥ 50 GB.
* Install Docker: `curl -fsSL https://get.docker.com | sudo sh` (installs the
  compose plugin for arm64 too).

## Network: two firewalls

OCI has a cloud firewall (security list / NSG) **and** Oracle's Ubuntu images ship
with host iptables rules that drop everything except SSH. Both must allow traffic.

| Port       | Proto | Purpose            | Suggested source |
|------------|-------|--------------------|------------------|
| 80         | tcp   | HTTP → HTTPS redirect, ACME | 0.0.0.0/0 |
| 443        | tcp   | dashboard / API    | 0.0.0.0/0 (or your IPs) |
| 443        | udp   | HTTP/3             | optional |
| 1935       | tcp   | RTMP publish (OBS) | your home/studio IP(s) |
| 8890       | udp   | SRT publish        | your home/studio IP(s) |

Host firewall (persisting the rule on Oracle images):

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp -m multiport --dports 80,443,1935 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p udp -m multiport --dports 443,8890 -j ACCEPT
sudo netfilter-persistent save
```

Docker publishes its ports via the `DOCKER` chain, which usually bypasses these
rules; the commands above are still needed for the ACME HTTP challenge on some
images. If `curl -I http://<ip>` hangs from outside, this is why.

## TLS

* Real hostname in `OMNI_DOMAIN` → Caddy gets a Let's Encrypt certificate on first
  start (port 80 must be reachable). Certificates live in the `caddy_data` volume.
* `localhost` or an IP → Caddy's internal CA. Your phone will warn; either accept
  once, or put the VM behind Tailscale and use its MagicDNS name.

## Egress / bandwidth (estimate)

Each destination receives a full copy of the ingest bitrate. 6 Mbps ingest × 5
destinations ≈ 30 Mbps sustained ≈ 13.5 GB/hour. OCI's Always Free tier includes
10 TB/month egress; more detail in Phase 6.

## Operations

```bash
make logs S=relay      # follow one service
make ps                # health of every service
docker compose pull && make up   # upgrade pinned images after bumping tags
```

Data lives in named volumes: `app_data` (SQLite), `redis_data`, `caddy_data`
(certificates). Phase 6 adds scheduled backups.
