from __future__ import annotations

import random

import pytest

from relay.ffmpeg import OutputSpec, Progress, build_command, output_format_args, redact
from relay.worker import _Samples, compute_backoff

INPUT = "rtsp://mediamtx:8554/live/secretkey"


def test_copy_profile_is_default_and_never_encodes() -> None:
    cmd = build_command("ffmpeg", INPUT, OutputSpec("rtmp://a.rtmp.youtube.com/live2/abcd"))
    assert cmd[0] == "ffmpeg"
    assert cmd[cmd.index("-i") + 1] == INPUT
    assert "-c" in cmd
    assert cmd[cmd.index("-c") + 1] == "copy"
    assert "libx264" not in cmd
    assert cmd[-1] == "rtmp://a.rtmp.youtube.com/live2/abcd"
    assert cmd[cmd.index("-f") + 1] == "flv"
    assert "no_duration_filesize" in cmd
    assert "-rtsp_transport" in cmd
    assert "-timeout" in cmd
    assert "-progress" in cmd
    assert cmd[cmd.index("-progress") + 1] == "pipe:1"


def test_vertical_profile_reencodes_to_9_16() -> None:
    cmd = build_command("ffmpeg", INPUT, OutputSpec("rtmps://x/app/k", "vertical_720p"))
    vf = cmd[cmd.index("-vf") + 1]
    assert "crop=ih*9/16:ih" in vf
    assert "scale=720:1280" in vf
    assert "libx264" in cmd
    assert "aac" in cmd
    assert "copy" not in cmd


def test_custom_profile_uses_given_args() -> None:
    spec = OutputSpec("srt://host:9000?streamid=x", "custom", "-c:v copy -c:a aac -b:a 96k")
    cmd = build_command("ffmpeg", INPUT, spec)
    assert cmd[cmd.index("-b:a") + 1] == "96k"
    assert cmd[cmd.index("-f") + 1] == "mpegts"


def test_custom_profile_requires_args() -> None:
    with pytest.raises(ValueError, match="custom"):
        build_command("ffmpeg", INPUT, OutputSpec("rtmp://x/y", "custom"))


def test_unknown_scheme_and_profile_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported output scheme"):
        output_format_args("http://example.com/x")
    with pytest.raises(ValueError, match="unknown profile"):
        build_command("ffmpeg", INPUT, OutputSpec("rtmp://x/y", "4k_hdr"))


def test_non_rtsp_input_has_no_rtsp_flags() -> None:
    cmd = build_command("ffmpeg", "srt://in:1/x", OutputSpec("rtmp://x/y"))
    assert "-rtsp_transport" not in cmd


def test_redact_hides_secrets() -> None:
    cmd = build_command("ffmpeg", INPUT, OutputSpec("rtmp://x/app/stream-key-123"))
    text = redact(cmd, ["stream-key-123", "secretkey"])
    assert "stream-key-123" not in text
    assert "secretkey" not in text
    assert "<redacted>" in text


def test_progress_parsing() -> None:
    p = Progress()
    lines = [
        "frame=10",
        "total_size=1234",
        "out_time_ms=500000",
        "speed=1.01x",
        "progress=continue",
    ]
    completed = [p.update(line) for line in lines]
    assert completed == [False, False, False, False, True]
    assert p.total_size == 1234
    assert p.out_time_us == 500000
    assert p.frame == 10
    assert p.speed == pytest.approx(1.01)
    assert p.ended is False
    assert p.update("garbage line without equals") is False
    assert p.update("total_size=N/A") is False
    assert p.total_size == 1234
    assert p.update("progress=end") is True
    assert p.ended is True


def test_backoff_doubles_and_caps() -> None:
    rng = random.Random(0)
    d1 = compute_backoff(1, 1.0, 60.0, rng)
    d2 = compute_backoff(2, 1.0, 60.0, rng)
    d4 = compute_backoff(4, 1.0, 60.0, rng)
    d20 = compute_backoff(20, 1.0, 60.0, rng)
    assert 1.0 <= d1 <= 1.2
    assert 2.0 <= d2 <= 2.4
    assert 8.0 <= d4 <= 9.6
    assert d20 == 60.0
    assert compute_backoff(0, 1.0, 60.0, rng) >= 1.0


def test_bitrate_window() -> None:
    s = _Samples(window=4.0)
    assert s.add(0.0, 0) == 0.0
    assert s.add(1.0, 125_000) == pytest.approx(1000.0)  # 125 kB/s = 1000 kbps
    assert s.add(2.0, 250_000) == pytest.approx(1000.0)
    assert s.add(10.0, 250_000) < 1000.0  # stalled => falls
    assert s.add(11.0, 100) == 0.0  # restart (counter reset) never goes negative
