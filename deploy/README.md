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

Each destination receives a full copy of the ingest bitrate (stream copy, no
re-encode), so egress ≈ ingest bitrate × enabled destinations. Chat traffic is
negligible.

| Ingest bitrate | Destinations | Sustained egress | Per hour | 4 h stream, 5 days/week, per month |
|----------------|--------------|------------------|----------|------------------------------------|
| 4.5 Mbps (1080p30) | 3 | 13.5 Mbps | 6.1 GB | ~0.5 TB |
| 6 Mbps (1080p60) | 5 | 30 Mbps | 13.5 GB | ~1.2 TB |
| 8 Mbps | 6 | 48 Mbps | 21.6 GB | ~1.9 TB |

OCI's Always Free tier includes 10 TB/month of outbound transfer; the A1 shape's
network bandwidth (1 Gbps per OCPU, capped) is not the bottleneck at these rates.
`vertical_720p` adds CPU (libx264 veryfast at 720x1280 is roughly one A1 core)
but not egress.

## Backups

```bash
make backup                 # snapshot inside the container + copy to ./backups/
ls backups/                 # omnistream-YYYYmmdd-HHMMSS.db
```

Schedule it on the host (`crontab -e`):

```
17 4 * * * cd /home/ubuntu/omnistream && make backup >> backups/backup.log 2>&1
```

Copy `backups/` **and `.env`** off the VM (OCI Object Storage, rclone, or scp);
the Fernet key in `.env` is required to read the stored stream keys and tokens.

Restore (stops api/relay/chat, validates the file, keeps a copy of the current
database in `/data/backups/prerestore-*.db`, restarts):

```bash
make restore FILE=backups/omnistream-YYYYmmdd-HHMMSS.db
```

Do not copy a backup into the volume by hand as root: SQLite then opens the
root-owned file read-only for the app user and every change fails with
"attempt to write a readonly database" (the detailed health report at
`/api/v1/health` flags this). If it already happened, `make restore` with the
same file fixes the ownership.

Chat history retention: `make prune` (default 30 days, `OMNI_CHAT_RETENTION_DAYS`).

## Metrics

`https://<domain>/metrics` serves Prometheus text format (Caddy proxies it to the API). From a scraper, set
`OMNI_METRICS_TOKEN` in `.env` and use `Authorization: Bearer <token>`; from a
browser the dashboard session is enough. Useful alerts:

* `omnistream_service_heartbeat_age_seconds > 60` (relay or chat process down)
* `omnistream_destination_status{status=~"error|retrying"} == 1` for 5 minutes
* `omnistream_ingest_live == 0` during a scheduled stream

## Security checklist

- [ ] Security list / NSG: 80, 443 tcp (+443 udp) from anywhere; 1935 tcp and
      8890 udp only from your streaming IPs; nothing else inbound except SSH.
- [ ] Host iptables rules from the Network section persisted.
- [ ] `.env` is mode 600 and never committed; `make check-config` passes.
- [ ] Stream key rotated if it was ever pasted somewhere public (`make secret`,
      update `OMNI_INGEST_STREAM_KEY`, `make up`).
- [ ] `docker compose pull && make up` monthly for base-image updates.

## Operations

```bash
make logs S=relay      # follow one service
make ps                # health of every service
docker compose pull && make up   # upgrade pinned images after bumping tags
```

Data lives in named volumes: `app_data` (SQLite), `redis_data`, `caddy_data`
(certificates). Phase 6 adds scheduled backups.
