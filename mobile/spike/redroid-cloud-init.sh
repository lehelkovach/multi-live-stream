#!/bin/bash
# Phase 5a spike: can redroid (Android in a container) run on an OCI Ampere VM?
#
# First-boot script for a throwaway VM (see docs/mobile-spike.md):
#   python deploy/oci/provision.py up --name redroid-spike --ocpus 2 --memory 8 \
#       --script mobile/spike/redroid-cloud-init.sh
#   python deploy/oci/provision.py status --name redroid-spike --follow
#   python deploy/oci/provision.py down --name redroid-spike --network --yes
#
# Every step reports an "OMNI: ..." line on the serial console and the script keeps
# going after a failed check, so one run fills in as much of the Findings table as
# it can. Full command output is in /var/log/redroid-spike.log on the VM.
set -uo pipefail

LOG=/var/log/redroid-spike.log
IMAGE="redroid/redroid:14.0.0_64only-latest"
SERIAL=127.0.0.1:5555
ANDROID_PATH=/product/bin:/apex/com.android.runtime/bin:/apex/com.android.art/bin:/system_ext/bin:/system/bin:/system/xbin:/odm/bin:/vendor/bin:/vendor/xbin
mark() { echo "OMNI: $*" | tee -a "$LOG" > /dev/console; }
quiet() { "$@" >> "$LOG" 2>&1; }
# a shell inside Android without depending on adb (adb is checked on its own below)
andro() { docker exec redroid /system/bin/sh -c "PATH=$ANDROID_PATH; $*" 2>> "$LOG" | tr -d '\r'; }

mark "start $(date -u +%FT%TZ) arch=$(uname -m) kernel=$(uname -r)" \
    "pagesize=$(getconf PAGESIZE) cpus=$(nproc) mem=$(free -g | awk '/^Mem/ {print $2}')G"
mark "kernel config: $(grep -hE '^CONFIG_(ANDROID_BINDER_IPC|ANDROID_BINDERFS|ASHMEM|DMABUF_HEAPS_SYSTEM)=' \
    "/boot/config-$(uname -r)" | tr '\n' ' ')"

export DEBIAN_FRONTEND=noninteractive
quiet apt-get update
# one at a time: a missing package (e.g. no modules-extra for a vendor kernel) must
# not stop the others from installing
missing=""
for pkg in sysstat adb "linux-modules-extra-$(uname -r)"; do
    quiet apt-get install -y "$pkg" || missing="$missing $pkg"
done
command -v adb > /dev/null || quiet apt-get install -y android-tools-adb
mark "packages: missing=[${missing# }] adb=$(command -v adb > /dev/null && adb version | head -1)"
if curl -fsSL https://get.docker.com | quiet sh; then
    mark "docker $(docker version --format '{{.Server.Version}}')"
else
    mark "docker install FAILED"
fi

if quiet modprobe binder_linux devices="binder,hwbinder,vndbinder"; then
    mark "binder_linux loaded"
else
    mark "binder_linux did not load"
fi
mark "binder: filesystems=$(grep -c binder /proc/filesystems)" \
    "devices=$(ls /dev/binder /dev/hwbinder /dev/vndbinder 2>/dev/null | tr '\n' ' ')"
if quiet modprobe ashmem_linux; then mark "ashmem_linux loaded"; else mark "no ashmem (memfd it is)"; fi
mark "dma_heap: $(ls /dev/dma_heap 2>/dev/null | tr '\n' ' ')"

t0=$(date +%s)
if quiet docker pull "$IMAGE"; then
    mark "pulled $IMAGE in $(($(date +%s) - t0))s ($(docker image inspect -f '{{.Size}}' "$IMAGE" | numfmt --to=iec))"
else
    mark "FAILED: could not pull $IMAGE"
    exit 1
fi
quiet docker run -d --name redroid --privileged \
    -v /srv/redroid:/data -p 127.0.0.1:5555:5555 "$IMAGE" \
    androidboot.use_memfd=true \
    androidboot.redroid_width=720 androidboot.redroid_height=1280 \
    androidboot.redroid_fps=30 androidboot.redroid_gpu_mode=guest

t0=$(date +%s)
booted=0
for _ in $(seq 1 72); do  # up to 6 minutes
    if [ "$(andro getprop sys.boot_completed)" = "1" ]; then booted=1; break; fi
    sleep 5
done
mark "boot_completed=$booted after $(($(date +%s) - t0))s" \
    "container=$(docker inspect -f '{{.State.Status}}' redroid 2>/dev/null)"
if [ "$booted" != "1" ]; then
    docker logs --tail 15 redroid 2>&1 | while read -r line; do mark "  log: $line"; done
    dmesg | grep -iE 'binder|redroid|denied' | tail -10 | while read -r line; do mark "  dmesg: $line"; done
    mark "FAILED: android did not finish booting"
    exit 1
fi

mark "android $(andro getprop ro.build.version.release) sdk=$(andro getprop ro.build.version.sdk)" \
    "abis=$(andro getprop ro.product.cpu.abilist)"
quiet adb connect "$SERIAL"
mark "adb: $(adb -s "$SERIAL" shell 'getprop ro.build.version.release; id -un' 2>&1 | tr -d '\r' | xargs)"
mark "cameras: $(andro dumpsys media.camera | grep -m1 -i 'number of camera devices' | xargs)" \
    "hal_providers=$(andro dumpsys media.camera | grep -ci 'provider')"
mark "focus: $(andro dumpsys window | grep -m1 mCurrentFocus | xargs)"
mark "settings app: $(andro am start -W -a android.settings.SETTINGS | grep -E 'Status|TotalTime' | xargs)"

sleep 30  # let the launch settle before measuring
busy=$(mpstat 60 1 | awk '/^Average/ {printf "%.1f", 100 - $NF}')
mark "cpu over 60s with Settings open: ${busy}% busy across $(nproc) cpus;" \
    "container $(docker stats --no-stream --format '{{.CPUPerc}} cpu {{.MemUsage}}' redroid)"
mark "screencap: $(docker exec redroid /system/bin/screencap -p 2>> "$LOG" | wc -c) bytes PNG"
mark "DONE spike finished $(date -u +%FT%TZ)"
