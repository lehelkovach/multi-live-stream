#!/bin/bash
# Phase 5a spike: can redroid (Android in a container) run on an OCI Ampere VM, and
# which Android version offers a way to give apps a camera?
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
# newest first: 15+ may ship AOSP's virtual camera HAL (option D in the spike doc)
VERSIONS="16.0.0 15.0.0 14.0.0"
SERIAL=127.0.0.1:5555
ANDROID_PATH=/product/bin:/apex/com.android.runtime/bin:/apex/com.android.art/bin:/system_ext/bin:/system/bin:/system/xbin:/odm/bin:/vendor/bin:/vendor/xbin
mark() { echo "OMNI: $*" | tee -a "$LOG" > /dev/console; }
quiet() { "$@" >> "$LOG" 2>&1; }
# a shell inside Android without depending on adb (adb is checked on its own below)
andro() { docker exec redroid /system/bin/sh -c "PATH=$ANDROID_PATH; $*" 2>> "$LOG" | tr -d '\r'; }
oneline() { tr '\n' ' ' | tr -s ' '; }  # (xargs would choke on quotes)

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

# One Android version at a time: boot it, check what the camera options need, remove it.
check_version() {
    local v=$1 tag="[$1]" image="redroid/redroid:$1_64only-latest" t0 booted
    t0=$(date +%s)
    if ! quiet docker pull "$image"; then
        mark "$tag could not pull $image"
        return
    fi
    mark "$tag pulled in $(($(date +%s) - t0))s ($(docker image inspect -f '{{.Size}}' "$image" | numfmt --to=iec))"
    quiet docker run -d --name redroid --privileged \
        -v "/srv/redroid-$v:/data" -p 127.0.0.1:5555:5555 "$image" \
        androidboot.use_memfd=true \
        androidboot.redroid_width=720 androidboot.redroid_height=1280 \
        androidboot.redroid_fps=30 androidboot.redroid_gpu_mode=guest
    t0=$(date +%s)
    booted=0
    for _ in $(seq 1 72); do  # up to 6 minutes
        if [ "$(andro getprop sys.boot_completed)" = "1" ]; then booted=1; break; fi
        sleep 5
    done
    mark "$tag boot_completed=$booted after $(($(date +%s) - t0))s" \
        "container=$(docker inspect -f '{{.State.Status}}' redroid 2>/dev/null)"
    if [ "$booted" = "1" ]; then
        mark "$tag android $(andro getprop ro.build.version.release) sdk=$(andro getprop ro.build.version.sdk)" \
            "type=$(andro getprop ro.build.type) debuggable=$(andro getprop ro.debuggable)" \
            "secure=$(andro getprop ro.secure) abis=$(andro getprop ro.product.cpu.abilist)"
        quiet adb connect "$SERIAL"
        mark "$tag adb shell user: $(adb -s "$SERIAL" shell id -un 2>&1 | tr -d '\r' | oneline)"
        # option A needs root inside Android
        mark "$tag adb root: $(adb -s "$SERIAL" root 2>&1 | tr -d '\r' | oneline)"
        sleep 3
        quiet adb connect "$SERIAL"
        mark "$tag adb user after root: $(adb -s "$SERIAL" shell id -un 2>&1 | tr -d '\r' | oneline)"
        mark "$tag cameras: $(andro dumpsys media.camera | grep -m1 -i 'number of camera devices' | oneline)"
        # option D: AOSP's virtual camera HAL and the virtual device manager behind it
        mark "$tag camera providers: $(andro service list | grep -i 'camera.provider' | oneline)"
        mark "$tag virtual camera service: $(andro service list | grep -iE 'virtual_?camera' | oneline)"
        mark "$tag virtual device manager: $(andro service check virtualdevice | oneline)"
        mark "$tag virtual camera binary: $(andro ls /system/bin/virtual_camera 2>&1 | oneline)"
        mark "$tag virtual camera flags: $(andro aflags list 2>/dev/null | grep -iE 'virtual_camera|virtualdevice.*camera' | head -5 | oneline)"
        mark "$tag focus: $(andro dumpsys window | grep -m1 mCurrentFocus | oneline)"
        sleep 20  # let it settle before measuring
        mark "$tag cpu over 30s: $(mpstat 30 1 | awk '/^Average/ {printf "%.1f", 100 - $NF}')% busy of $(nproc) cpus;" \
            "container $(docker stats --no-stream --format '{{.CPUPerc}} cpu {{.MemUsage}}' redroid)"
    else
        docker logs --tail 10 redroid 2>&1 | while read -r line; do mark "$tag log: $line"; done
    fi
    quiet adb disconnect "$SERIAL"
    quiet docker rm -f redroid
    quiet docker image rm "$image"  # keep the boot volume from filling up
}

for v in $VERSIONS; do
    check_version "$v"
done
mark "DONE spike finished $(date -u +%FT%TZ)"
