# Phase 5a spike: streaming into mobile-only apps (Redroid + Appium)

Status: **desk research + runbook, 2026-10-25; Redroid boot verified on OCI
2026-11-05 (see Findings).** The hands-on part (booting Redroid
on the OCI Ampere VM and feeding it video) can only be done on the VM itself, so
this document records what the current docs say, the exact commands to run, what
to measure, and the go/no-go criteria. Results go into the "Findings" section at
the end as they come in. Nothing in `mobile/` is built until that section says go.

## 1. Redroid on arm64 OCI

Redroid (Remote Android) runs Android userspace in a privileged container on the
host kernel; no KVM is needed, which is why it is the candidate for an Ampere VM.

What the redroid docs require of the host (kernel features): **binderfs**,
**ashmem or memfd**, IPv6, ION / DMA-BUF heaps, 4 KB pages. Ubuntu 22.04/24.04
arm64 kernels on OCI are generic Ubuntu kernels, so the expected path is the
documented Ubuntu one:

```bash
# on the VM (Ubuntu 22.04/24.04 arm64)
sudo apt install -y "linux-modules-extra-$(uname -r)"
sudo modprobe binder_linux devices="binder,hwbinder,vndbinder"
grep -c binder /proc/filesystems                       # expect 1
sudo modprobe ashmem_linux || echo "no ashmem (removed since kernel 5.18) -> use memfd"
# persist
echo 'binder_linux' | sudo tee /etc/modules-load.d/redroid.conf
echo 'options binder_linux devices=binder,hwbinder,vndbinder' | sudo tee /etc/modprobe.d/redroid.conf
```

Boot one container (arm64 needs the `_64only` images):

```bash
docker run -itd --name redroid14 --privileged --pull always \
  -v /srv/redroid14:/data -p 127.0.0.1:5555:5555 \
  redroid/redroid:14.0.0_64only-latest \
  androidboot.use_memfd=true \
  androidboot.redroid_width=720 androidboot.redroid_height=1280 \
  androidboot.redroid_fps=30 androidboot.redroid_gpu_mode=guest
adb connect 127.0.0.1:5555 && adb shell getprop ro.build.version.release
scrcpy -s 127.0.0.1:5555            # optional: look at the screen from a laptop over Tailscale
```

Notes from the docs that matter for us:

* `androidboot.use_memfd=true` replaces ashmem (which Ubuntu 24.04's 6.8 kernel no
  longer ships); the docs say memfd will become the default.
* `redroid_gpu_mode=guest` is software rendering; the A1 shape has no GPU, so
  expect the CPU to carry decoding + rendering. **Measure** CPU at 720x1280/30 fps.
* No Google services by default (Open GApps / MicroG / MindTheGapps are documented
  add-ons). Apps that insist on Play Services will need MicroG at least.
* `ro.secure=0` gives a root adb shell. Root is useful for us (virtual camera
  module) but is also what detection libraries look for.
* Never expose 5555 publicly: bind to localhost and reach it over Tailscale or
  from the Appium container on the same Compose network.

Fallback if the module route fails on the OCI kernel: the `redroid-modules`
repository builds binder/ashmem out of tree, or a mainline kernel with
`CONFIG_ANDROID_BINDERFS=y`.

## 2. Getting our stream into the app as "the camera"

This is the real risk. Redroid ships **no camera HAL**, and v4l2loopback devices
on the host are not seen by Android HALs (reports of exactly this on Android 9
boards: the loopback node is not recognised as a system camera). Options, in the
order they should be tried:

| # | Approach | How | Risk / cost |
|---|----------|-----|-------------|
| A | **Virtual camera app/module inside Android** (e.g. LSPosed + a "virtual camera" module that replaces the camera preview with a video source) | Root is available in redroid; the module reads a video file or RTMP/RTSP URL; we point it at `rtsp://mediamtx:8554/live/<key>` (same source the relay uses) | Depends on a third-party module keeping up with the target app's camera API (Camera2/CameraX); detection libraries flag Xposed/LSPosed |
| B | **Build a camera HAL that reads a stream** (patched `camera.default` HAL or Android's emulated/"virtual camera" HAL fed from a socket) | Needs an Android build; redroid provides `android-builder-docker` | Days of work; most robust and least detectable once done |
| C | **Screen-level injection**: run the app on a *real* phone and use the phone's own camera pointed at a display | No software hack; zero detection risk | Needs hardware; latency and quality loss; not a server-side solution |
| D | **Android 14+ `VirtualCamera` / `VirtualDeviceManager` APIs** | Platform API for virtual cameras exists from Android 14 for companion/device-streaming use cases | Requires privileged permissions; feasibility inside redroid unknown |

**Spike order:** A first (one afternoon: install LSPosed, try a maintained
virtual-camera module, open the target app, see whether the preview shows our
RTSP source). If the app shows a black preview or refuses, test D on the 15.0
image, then decide whether B is worth it.

Pre-work that is independent of the camera question and can be done now:

* Appium + UiAutomator2 against redroid over adb (`appium driver install
  uiautomator2`), scripted on arm64 (`appium/appium` images are multi-arch).
* Reading chat via the accessibility tree: `driver.page_source` / UiAutomator2
  selectors on the app's chat list, diffing by text + author per poll. OCR only if
  the app draws chat on a canvas/SurfaceView.

## 3. Target apps: detection and terms of service

Check for each app **before** investing in automation. For the first target
(MeetMe, per the project brief) the public docs don't state root/emulator
policies; the only way to know is to install it in redroid and watch for
"unsupported device" screens or silent feature loss. Record the outcome in
"Findings". General expectations from app-security practice: streaming apps
commonly bundle root/emulator detection and may restrict features, require extra
verification, or terminate. Operating an account from an automated Android
container may breach the app's terms; that is the owner's call and should be
documented per app here.

Checklist per app:

- [ ] Installs and signs in on redroid (with/without MicroG).
- [ ] Any "rooted/emulated device" message (with `ro.secure=0`, with LSPosed).
- [ ] Camera preview shows the injected video; "Go live" works.
- [ ] Chat is readable from the UI tree; typing into the chat box works.
- [ ] Terms of service: quote the clause on automation/emulators and the decision.

## 4. Fallback: real phones over ADB + Tailscale

Same automation, different device: one or more cheap Android phones on Wi-Fi at
home, Tailscale on the phone (or a Tailscale subnet router), `adb tcpip 5555` and
`adb connect <tailscale-ip>:5555` from the VM. The phone's real camera films a
monitor showing the stream (option C), or a per-app virtual camera module is used
on a rooted phone (option A on real hardware, lower detection risk). Appium flows
are identical; only the device address changes. This is also the path if the OCI
kernel cannot run redroid at all.

## 5. Go / no-go for 5b

Go when all of these hold for **one** app: redroid boots on the VM with < 2 CPU
cores at 720p30 idle+preview, the app shows our video as its camera, going live
works for 30 minutes unattended, chat is readable from the UI tree, and the ToS
risk is accepted in writing in this file. Otherwise: fall back to real phones
(section 4) or park Phase 5.

## 6. Findings (fill in on the VM)

Steps 1 and the camera check run unattended on a throwaway VM, no SSH needed:

```bash
python deploy/oci/provision.py up --name redroid-spike --ocpus 2 --memory 8 \
    --script mobile/spike/redroid-cloud-init.sh
python deploy/oci/provision.py status --name redroid-spike --follow   # results, ~10 min
python deploy/oci/provision.py down --name redroid-spike --network --yes
```

It reports kernel config, binder/memfd/dma-heap availability, Redroid 14 pull and
boot time, the Android version, how many cameras Android sees, and CPU use with an
app open. The virtual-camera, app sign-in and 30-minute rows need hands-on work.

| Date | Step | Result | Notes |
|------|------|--------|-------|
| 2026-11-05 | modprobe binder_linux on OCI Ubuntu 24.04 arm64 | **works** | kernel `7.0.0-1013-oracle` (VM.Standard.A1.Flex, 2 OCPU / 8 GB), 4 KB pages. `CONFIG_ANDROID_BINDER_IPC=m`, `CONFIG_ANDROID_BINDERFS=m`: binder ships in the base modules, and there is no `linux-modules-extra` package for this kernel (none needed). No ashmem, so `androidboot.use_memfd=true`; `/dev/dma_heap/system` present. |
| 2026-11-05 | redroid 14 `_64only` boots, adb connects | **works** | Image pull 31 s (2.2 GB). `sys.boot_completed=1` 15 s after `docker run`; Android 14, SDK 34, `arm64-v8a`; launcher in focus; Settings opens in 520 ms; `adb shell` connects as user `shell` (root not checked yet). CPU 0.4 % of 2 cores, 584 MiB RAM with Settings on screen. That is a **static** screen (Android only redraws on change), so it says nothing about the 720p30 preview load in the go/no-go. |
| 2026-11-05 | camera presence | **none, as expected** | `dumpsys media.camera`: "Number of camera devices: 0", no camera HAL provider. Confirms section 2: apps will find no camera until option A, B or D supplies one. |
| 2026-11-05 | root (needed by option A) | **available** | Redroid 14 and 15 images are `userdebug` (`ro.debuggable=1`); `adb root` switches adbd to root. |
| 2026-11-05 | option D pieces (AOSP virtual camera) | **present on Android 15**, absent on 14 | Redroid 15 (SDK 35, boots in 21 s, 875 MiB): `/system/bin/virtual_camera` exists; aconfig flags `android.companion.virtual.flags.virtual_camera`, `android.companion.virtualdevice.flags.camera_device_awareness` and `.virtual_camera_service_discovery` are enabled; the `virtualdevice` service is running. No camera provider is registered until a virtual device creates a camera, so the camera count is still 0. |
| 2026-11-05 | Redroid 16 | **did not boot** | Same parameters: `sys.boot_completed` still 0 after 6 min, container running, no container logs. Not investigated; 15 has what option D needs. |
|      | virtual camera option A | | module/version: |
|      | target app sign-in + detection | | |
|      | go live 30 min | | |

**So far:** Redroid itself is not the obstacle on OCI Ampere: it boots fast and idles
near zero. The open question is the one section 2 predicted: getting a camera into
Android. Both routes are open on Redroid 15:

* **Option D (preferred): AOSP's virtual camera.** A real camera HAL fed through a
  Surface, no hooks in the target app. Creating a virtual device needs a system app
  holding a companion-device role with a CDM association. On this rooted image we can
  install our own small "camera bridge" app as a system app and grant both from the
  root shell. The bridge decodes our RTSP stream (`MediaCodec`) into the virtual
  camera's Surface. Catch: with `camera_device_awareness`, only apps running on the
  virtual device's display see that camera, so the target app must be launched
  there, and Appium has to drive it on that display.
* **Option A (fallback): camera-hook modules** (LSPosed + VCAM/CamSwap-style). Root is
  available. But hooks are what detection libraries look for, and the modules are
  small unvetted projects.

**Next, without the owner:** build the bridge app and prove a stock camera app on the
virtual display shows our test pattern. **Then, with the owner:** the target app (which
app, its APK, an account, and the ToS decision in section 3).
