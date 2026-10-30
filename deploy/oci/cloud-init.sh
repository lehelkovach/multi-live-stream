#!/bin/bash
# OmniStream bootstrap for a fresh Oracle Cloud Ubuntu 24.04 arm64 VM.
#
# deploy/oci/provision.py fills in the __PLACEHOLDERS__ and passes this file as
# cloud-init user_data. Progress lines start with "OMNI:" and go to the serial
# console, which `provision.py status` reads back through the OCI API.
#
# Secrets are never printed: the admin password arrives only as an argon2 hash,
# and the encryption key, session secret and stream key are generated here by
# scripts/init_env.py (the stream key is shown in the dashboard's OBS settings).
set -euo pipefail

REPO_URL="__REPO_URL__"
BRANCH="__BRANCH__"
ADMIN_PASSWORD_HASH='__ADMIN_PASSWORD_HASH__'
DOMAIN_OVERRIDE="__DOMAIN__"
APP_DIR=/opt/omnistream
LOG=/var/log/omnistream-bootstrap.log

mark() { echo "OMNI: $*" | tee -a "$LOG" > /dev/console; }
fail() { mark "FAILED: $*"; exit 1; }
trap 'fail "line $LINENO (exit $?)"' ERR

mark "start $(date -u +%FT%TZ) arch=$(uname -m)"
export DEBIAN_FRONTEND=noninteractive
APT=(apt-get -o DPkg::Lock::Timeout=900 -y -q)

# Oracle's Ubuntu images drop everything but SSH in the host firewall (and the
# VCN security list is a second, separate firewall). Insert ACCEPTs before
# Oracle's final REJECT and persist them before Docker adds its own chains.
mark "firewall: 80,443,1935/tcp 443,8890/udp"
for rule in "-p tcp -m multiport --dports 80,443,1935" "-p udp -m multiport --dports 443,8890"; do
  pos=$(iptables -L INPUT --line-numbers -n | awk '$2=="REJECT"{print $1; exit}')
  # shellcheck disable=SC2086  # word splitting of $rule is intended
  if [ -n "$pos" ]; then
    iptables -I INPUT "$pos" -m state --state NEW $rule -j ACCEPT
  else
    iptables -A INPUT -m state --state NEW $rule -j ACCEPT
  fi
done
if command -v netfilter-persistent > /dev/null; then netfilter-persistent save > /dev/null 2>&1 || true; fi

mark "packages"
"${APT[@]}" update > /dev/null
"${APT[@]}" install git make ca-certificates curl > /dev/null
if ! command -v docker > /dev/null; then
  mark "installing docker"
  curl -fsSL https://get.docker.com | sh > /var/log/omnistream-docker-install.log 2>&1
fi
systemctl enable --now docker > /dev/null 2>&1
usermod -aG docker ubuntu || true
mark "docker $(docker version --format '{{.Server.Version}}') compose $(docker compose version --short)"

mark "code $REPO_URL @ $BRANCH"
if [ ! -d "$APP_DIR/.git" ]; then
  git clone --quiet --depth 1 --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"
mark "commit $(git rev-parse --short HEAD)"

if [ -n "$DOMAIN_OVERRIDE" ]; then
  DOMAIN="$DOMAIN_OVERRIDE"
else
  # sslip.io maps 1-2-3-4.sslip.io to 1.2.3.4, so Caddy can get a real
  # Let's Encrypt certificate without owning a domain.
  IP=$(curl -fsS --max-time 15 https://checkip.amazonaws.com | tr -d '[:space:]')
  [ -n "$IP" ] || fail "could not determine the public IP"
  DOMAIN="${IP//./-}.sslip.io"
fi
mark "domain $DOMAIN"

if [ ! -f .env ]; then
  python3 scripts/init_env.py --domain "$DOMAIN" > /dev/null  # it prints secrets: discard
  if [ -n "$ADMIN_PASSWORD_HASH" ]; then
    python3 - "$ADMIN_PASSWORD_HASH" << 'PY'
import re
import sys
from pathlib import Path

env = Path(".env")
text = env.read_text()
text = re.sub(r"(?m)^OMNI_ADMIN_PASSWORD=.*$", "OMNI_ADMIN_PASSWORD=", text)
# single quotes: Docker Compose would otherwise expand the $ signs in the hash
text = re.sub(r"(?m)^OMNI_ADMIN_PASSWORD_HASH=.*$", lambda _: f"OMNI_ADMIN_PASSWORD_HASH='{sys.argv[1]}'", text)
env.write_text(text)
PY
  fi
  chmod 600 .env
fi
chown -R ubuntu:ubuntu "$APP_DIR"

mark "building and starting the stack (first arm64 build takes a while)"
if ! docker compose up -d --build > /var/log/omnistream-compose-up.log 2>&1; then
  tail -30 /var/log/omnistream-compose-up.log | sed 's/^/OMNI: build| /' > /dev/console
  fail "docker compose up"
fi

mark "waiting for health checks"
for _ in $(seq 1 60); do
  pending=$(docker compose ps --format '{{.Service}} {{.Health}}' | awk '$2 != "" && $2 != "healthy" {print $1}')
  [ -z "$pending" ] && break
  sleep 5
done
docker compose ps --format 'OMNI: service {{.Service}} {{.State}} {{.Health}}' > /dev/console

# Smoke test from inside the Compose network, using the app image's ffmpeg.
key=$(grep -E '^OMNI_INGEST_STREAM_KEY=' .env | cut -d= -f2-)
publish() {
  docker compose run --rm --no-deps -T --entrypoint ffmpeg api -nostdin -loglevel error -re \
    -f lavfi -i testsrc=size=640x360:rate=30 -c:v libx264 -preset ultrafast -tune zerolatency \
    -g 60 -pix_fmt yuv420p -t "$2" -f flv "rtmp://mediamtx:1935/live/$1" > /dev/null 2>&1
}
wrong="no"; publish "not-the-key" 3 || wrong="yes"
publish "$key" 25 &
pub_pid=$!
sleep 12
ready=$(docker compose exec -T api curl -fsS http://mediamtx:9997/v3/paths/list 2> /dev/null \
  | python3 -c 'import json,sys; print(sum(1 for p in json.load(sys.stdin)["items"] if p["ready"]))' 2> /dev/null || echo 0)
relay_live=$(docker compose exec -T redis redis-cli GET omni:ingest:state 2> /dev/null \
  | python3 -c 'import json,sys; print("yes" if json.loads(sys.stdin.read() or "{}").get("live") else "no")' 2> /dev/null || echo no)
wait "$pub_pid" || true
mark "smoke wrong_key_rejected=$wrong ingest_ready_paths=$ready relay_sees_live=$relay_live"

tls="pending"
for _ in $(seq 1 24); do
  if curl -fsS --max-time 10 "https://$DOMAIN/healthz" > /dev/null 2>&1; then tls="trusted"; break; fi
  sleep 5
done
health=$(curl -fsSk --max-time 10 --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN/healthz" 2> /dev/null || echo "unreachable")
mark "edge healthz=$health tls=$tls"
mark "DONE url=https://$DOMAIN"
