"""DestinationWorker lifecycle against a fake ffmpeg (tests/fixtures/fake_ffmpeg.py)."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from core.models import DestinationStatus
from relay.worker import DestinationWorker, WorkerConfig, WorkerStatus

FAKE = str(Path(__file__).resolve().parent.parent / "fixtures" / "fake_ffmpeg.py")


def fake_cmd(mode: str, **kw: float) -> list[str]:
    cmd = [sys.executable, "-I", FAKE, "--mode", mode]
    for k, v in kw.items():
        cmd += [f"--{k}", str(v)]
    return cmd


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, bool]] = []
        self.statuses: list[WorkerStatus] = []

    async def __call__(self, status: WorkerStatus, changed: bool) -> None:
        self.events.append((status.status, changed))
        self.statuses.append(WorkerStatus(**status.to_dict()))

    def transitions(self) -> list[str]:
        out: list[str] = []
        for s, changed in self.events:
            if changed and (not out or out[-1] != s):
                out.append(s)
        return out


async def wait_for(predicate: Callable[[], bool], within: float = 10.0, step: float = 0.05) -> None:
    deadline = asyncio.get_running_loop().time() + within
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("condition not met in time")
        await asyncio.sleep(step)


FAST = WorkerConfig(
    backoff_base=0.1, backoff_max=0.5, stall_timeout=1.0, healthy_after=0.6, error_after_failures=3
)


async def test_happy_path_goes_live_and_reports_stats_then_stops_cleanly() -> None:
    rec = Recorder()
    w = DestinationWorker(1, "yt", fake_cmd("ok"), config=FAST, on_status=rec)
    w.start()
    await wait_for(lambda: w.status.status == DestinationStatus.LIVE)
    await wait_for(lambda: w.status.bytes_sent > 60_000)
    assert w.status.pid is not None
    assert w.status.bitrate_kbps >= 0
    await w.stop()
    assert not w.running
    assert w.status.to_dict()["pid"] is None
    assert w.status.restarts == 0
    assert rec.transitions()[:2] == [DestinationStatus.STARTING, DestinationStatus.LIVE]
    assert w.status.last_error is None


async def test_immediate_failure_retries_with_backoff_then_error() -> None:
    rec = Recorder()
    w = DestinationWorker(2, "fb", fake_cmd("fail"), config=FAST, on_status=rec)
    w.start()
    await wait_for(lambda: w.status.consecutive_failures >= FAST.error_after_failures, within=15)
    await w.stop()
    assert w.status.status == DestinationStatus.ERROR
    assert w.status.restarts >= FAST.error_after_failures - 1
    assert w.status.last_error is not None
    assert "exited with code 1" in w.status.last_error
    assert "Cannot open connection" in w.status.last_error
    assert DestinationStatus.RETRYING in rec.transitions()
    assert w.status.next_retry_at is not None


async def test_exit_after_healthy_run_resets_failure_counter() -> None:
    w = DestinationWorker(3, "tw", fake_cmd("exit-after", seconds=0.8), config=FAST)
    w.start()
    await wait_for(lambda: w.status.status == DestinationStatus.LIVE)
    await wait_for(lambda: w.status.restarts >= 1, within=10)
    assert w.status.consecutive_failures == 1  # healthy run (>= healthy_after) => counter reset
    await wait_for(lambda: w.status.status == DestinationStatus.LIVE and w.status.restarts >= 1)
    await w.stop()
    assert "Broken pipe" in (w.status.last_error or "")


async def test_stall_is_detected_and_restarted() -> None:
    w = DestinationWorker(4, "kick", fake_cmd("stall"), config=FAST)
    w.start()
    await wait_for(lambda: w.status.status == DestinationStatus.LIVE)
    await wait_for(lambda: w.status.restarts >= 1, within=10)
    assert "no output progress" in (w.status.last_error or "")
    await w.stop()


async def test_killed_process_is_restarted() -> None:
    w = DestinationWorker(5, "x", fake_cmd("ok"), config=FAST)
    w.start()
    await wait_for(lambda: w.status.status == DestinationStatus.LIVE)
    pid = w.status.pid
    assert pid is not None
    os.kill(pid, signal.SIGKILL)
    await wait_for(
        lambda: w.status.restarts >= 1 and w.status.status == DestinationStatus.LIVE, within=10
    )
    assert w.status.pid not in (None, pid)
    await w.stop()


async def test_missing_binary_reports_error_without_crashing() -> None:
    w = DestinationWorker(6, "bad", ["/nonexistent/ffmpeg-binary"], config=FAST)
    w.start()
    await wait_for(lambda: w.status.consecutive_failures >= 2, within=10)
    assert "cannot start ffmpeg" in (w.status.last_error or "")
    await w.stop()


async def test_stop_during_backoff_returns_promptly() -> None:
    cfg = WorkerConfig(backoff_base=30.0, backoff_max=60.0)
    w = DestinationWorker(7, "slowretry", fake_cmd("fail"), config=cfg)
    w.start()
    await wait_for(lambda: w.status.status == DestinationStatus.RETRYING)
    await asyncio.wait_for(w.stop(), timeout=3)
    assert not w.running


async def test_secrets_redacted_in_error() -> None:
    cmd = [*fake_cmd("fail"), "rtmp://x/app/supersecretkey"]
    w = DestinationWorker(8, "s", cmd, config=FAST, secrets=["supersecretkey"])
    w.start()
    await wait_for(lambda: w.status.last_error is not None)
    await w.stop()
    assert "supersecretkey" not in (w.status.last_error or "")


async def test_clean_end_of_input_waits_for_the_supervisor() -> None:
    """ffmpeg exits 0 when the publisher disconnects: no failure, no blind retry."""
    cfg = WorkerConfig(backoff_base=0.1, backoff_max=0.5, input_ended_wait=30.0)
    rec = Recorder()
    w = DestinationWorker(9, "end", fake_cmd("end", seconds=0.3), config=cfg, on_status=rec)
    w.start()
    await wait_for(lambda: w.status.restarts >= 1, within=10)
    await wait_for(lambda: w.status.status == DestinationStatus.IDLE)
    assert w.status.last_error == "input stream ended"
    assert w.status.consecutive_failures == 0
    assert w.status.next_retry_at is None
    await asyncio.sleep(0.6)  # without a poke it does not restart by itself
    assert w.status.status == DestinationStatus.IDLE
    assert w.status.pid is None
    w.poke()  # publisher is back
    await wait_for(lambda: w.status.status == DestinationStatus.LIVE, within=10)
    assert DestinationStatus.RETRYING not in rec.transitions()
    await asyncio.wait_for(w.stop(), timeout=5)


async def test_stop_while_waiting_for_input_returns_promptly() -> None:
    cfg = WorkerConfig(input_ended_wait=60.0)
    w = DestinationWorker(10, "end2", fake_cmd("end", seconds=0.2), config=cfg)
    w.start()
    await wait_for(
        lambda: w.status.status == DestinationStatus.IDLE and w.status.restarts >= 1, within=10
    )
    await asyncio.wait_for(w.stop(), timeout=3)
    assert not w.running


async def test_poke_does_not_shortcut_failure_backoff() -> None:
    cfg = WorkerConfig(backoff_base=30.0, backoff_max=60.0)
    w = DestinationWorker(11, "failing", fake_cmd("fail"), config=cfg)
    w.start()
    await wait_for(lambda: w.status.status == DestinationStatus.RETRYING)
    restarts = w.status.restarts
    w.poke()
    await asyncio.sleep(0.5)
    assert w.status.restarts == restarts  # still backing off: pokes are for clean ends only
    await asyncio.wait_for(w.stop(), timeout=3)
