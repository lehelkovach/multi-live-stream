"""Build ffmpeg command lines for relay outputs and parse ``-progress`` output.

The default profile is stream copy: no decoding, no encoding, ~zero CPU. Other
profiles re-encode and are opt-in per destination.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from urllib.parse import urlsplit

from core.models import Profile

FLV_SCHEMES = ("rtmp", "rtmps")
MPEGTS_SCHEMES = ("srt", "udp")


@dataclass(frozen=True)
class OutputSpec:
    url: str  # full output URL including the stream key
    profile: str = Profile.COPY
    custom_args: str | None = None


def output_format_args(url: str) -> list[str]:
    scheme = urlsplit(url).scheme
    if scheme in FLV_SCHEMES:
        return ["-f", "flv", "-flvflags", "no_duration_filesize"]
    if scheme in MPEGTS_SCHEMES:
        return ["-f", "mpegts"]
    msg = f"unsupported output scheme {scheme!r} in {url!r}"
    raise ValueError(msg)


def profile_args(spec: OutputSpec) -> list[str]:
    if spec.profile == Profile.COPY:
        return ["-c", "copy"]
    if spec.profile == Profile.VERTICAL_720P:
        return [
            # centre-crop to 9:16, then scale to 720x1280
            "-vf",
            "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=720:1280:flags=bicubic",
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-profile:v", "high", "-pix_fmt", "yuv420p",
            "-b:v", "2500k", "-maxrate", "2500k", "-bufsize", "5000k",
            "-force_key_frames", "expr:gte(t,n_forced*2)", "-sc_threshold", "0",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        ]  # fmt: skip
    if spec.profile == Profile.CUSTOM:
        if not spec.custom_args:
            msg = "custom profile needs ffmpeg args"
            raise ValueError(msg)
        return shlex.split(spec.custom_args)
    msg = f"unknown profile {spec.profile!r}"
    raise ValueError(msg)


def build_command(
    ffmpeg_bin: str, input_url: str, spec: OutputSpec, *, rtsp_timeout_s: float = 10.0
) -> list[str]:
    """Full argv for one relay worker. Progress goes to stdout, diagnostics to stderr."""
    cmd = [
        ffmpeg_bin,
        "-hide_banner", "-nostdin", "-loglevel", "warning", "-nostats",
        "-progress", "pipe:1", "-stats_period", "1",
    ]  # fmt: skip
    if urlsplit(input_url).scheme in ("rtsp", "rtsps"):
        # -timeout (us) is the RTSP socket I/O timeout (ffmpeg >= 5.0)
        cmd += ["-rtsp_transport", "tcp", "-timeout", str(int(rtsp_timeout_s * 1_000_000))]
    cmd += ["-i", input_url]
    cmd += profile_args(spec)
    cmd += output_format_args(spec.url)
    cmd.append(spec.url)
    return cmd


def redact_text(text: str, secrets: list[str]) -> str:
    """``text`` with every secret (stream keys) replaced by ``<redacted>``."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def redact(cmd: list[str], secrets: list[str]) -> str:
    """Printable, shell-quoted command with stream keys replaced, for logs."""
    return redact_text(shlex.join(cmd), secrets)


@dataclass
class Progress:
    """Parsed state from ffmpeg ``-progress`` key=value blocks."""

    total_size: int = 0
    out_time_us: int = 0
    frame: int = 0
    speed: float | None = None
    ended: bool = False

    def update(self, line: str) -> bool:
        """Apply one ``key=value`` line. Returns True when a block completed (``progress=``)."""
        key, sep, value = line.strip().partition("=")
        if not sep:
            return False
        value = value.strip()
        if key == "total_size" and value.isdigit():
            self.total_size = int(value)
        elif key in ("out_time_us", "out_time_ms") and value.lstrip("-").isdigit():
            # ffmpeg historically emitted microseconds under the name out_time_ms
            self.out_time_us = int(value)
        elif key == "frame" and value.isdigit():
            self.frame = int(value)
        elif key == "speed":
            try:
                self.speed = float(value.rstrip("x"))
            except ValueError:
                self.speed = None
        elif key == "progress":
            self.ended = value == "end"
            return True
        return False
