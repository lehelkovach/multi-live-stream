"""One supervised ffmpeg process for one destination.

Lifecycle: ``starting`` -> ``live`` (first output progress) -> on exit ``retrying``
(exponential backoff) or ``error`` (many consecutive failures; still retrying at the
backoff cap). A worker never gives up on its own; the supervisor stops it when the
destination is disabled, deleted, or the input goes offline.

Isolation: each worker is its own asyncio task + its own OS process in its own
session. A crash here is contained to this destination.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import random
import signal
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from core.logging import get_logger
from core.models import DestinationStatus
from relay.ffmpeg import Progress, redact

log = get_logger(__name__)

StatusCallback = Callable[["WorkerStatus", bool], Awaitable[None]]


@dataclass(frozen=True)
class WorkerConfig:
    backoff_base: float = 1.0
    backoff_max: float = 60.0
    stall_timeout: float = 20.0
    healthy_after: float = 30.0
    error_after_failures: int = 5
    stop_grace: float = 5.0
    stats_interval: float = 2.0


@dataclass
class WorkerStatus:
    id: int
    name: str
    status: str = DestinationStatus.STARTING
    last_error: str | None = None
    pid: int | None = None
    started_at: str | None = None
    uptime_s: float = 0.0
    bitrate_kbps: float = 0.0
    bytes_sent: int = 0
    restarts: int = 0
    consecutive_failures: int = 0
    next_retry_at: str | None = None
    detail: str | None = None  # human hint for inactive states ("no input stream", ...)
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        self.updated_at = _now_iso()
        return asdict(self)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _set_pdeathsig() -> None:
    """Make the child die with us (Linux only) so a relay crash never leaves orphans
    pushing to a platform while a fresh relay starts a second copy."""
    if sys.platform != "linux":
        return
    with contextlib.suppress(Exception):
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        pr_set_pdeathsig = 1
        libc.prctl(pr_set_pdeathsig, int(signal.SIGKILL), 0, 0, 0)


_default_rng = random.Random()


def compute_backoff(
    failures: int, base: float, maximum: float, rng: random.Random | None = None
) -> float:
    """Exponential backoff with up to 20% jitter: base, 2*base, 4*base ... capped."""
    failures = max(1, failures)
    delay: float = min(base * (2 ** (failures - 1)), maximum)
    jitter: float = (rng or _default_rng).uniform(0.0, 0.2 * delay)
    return min(delay + jitter, maximum)


@dataclass
class _Samples:
    """(monotonic time, bytes) ring used to compute a short-window bitrate."""

    window: float = 4.0
    points: deque[tuple[float, int]] = field(default_factory=deque)

    def add(self, t: float, total: int) -> float:
        self.points.append((t, total))
        while self.points and t - self.points[0][0] > self.window and len(self.points) > 2:
            self.points.popleft()
        t0, b0 = self.points[0]
        if t - t0 <= 0 or total < b0:
            return 0.0
        return (total - b0) * 8 / (t - t0) / 1000.0


class DestinationWorker:
    def __init__(
        self,
        dest_id: int,
        name: str,
        cmd: list[str],
        *,
        config: WorkerConfig | None = None,
        on_status: StatusCallback | None = None,
        secrets: list[str] | None = None,
    ) -> None:
        self.dest_id = dest_id
        self.name = name
        self.cmd = list(cmd)
        self.config = config or WorkerConfig()
        self._on_status = on_status
        self._secrets = secrets or []
        self.status = WorkerStatus(id=dest_id, name=name)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._log = log.bind(dest_id=dest_id, dest=name)

    # ---- public --------------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"relay-worker-{self.dest_id}")

    async def stop(self) -> None:
        self._stop.set()
        await self._terminate_process()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ---- internals -----------------------------------------------------------

    async def _emit(self, changed: bool) -> None:
        if self._on_status is None:
            return
        try:
            await self._on_status(self.status, changed)
        except Exception as exc:
            self._log.warning("worker.status_callback_failed", error=str(exc))

    async def _set_status(
        self, status: str, *, error: str | None = None, force: bool = False
    ) -> None:
        changed = (
            force
            or status != self.status.status
            or (error is not None and error != self.status.last_error)
        )
        self.status.status = status
        if error is not None:
            self.status.last_error = error[:2000]
        await self._emit(changed)

    async def _run(self) -> None:
        try:
            while not self._stop.is_set():
                await self._run_once()
                if self._stop.is_set():
                    break
                delay = compute_backoff(
                    self.status.consecutive_failures,
                    self.config.backoff_base,
                    self.config.backoff_max,
                )
                self.status.restarts += 1
                self.status.next_retry_at = datetime.fromtimestamp(
                    time.time() + delay, UTC
                ).isoformat()
                status = (
                    DestinationStatus.ERROR
                    if self.status.consecutive_failures >= self.config.error_after_failures
                    else DestinationStatus.RETRYING
                )
                self._log.warning(
                    "worker.retrying",
                    delay_s=round(delay, 1),
                    failures=self.status.consecutive_failures,
                    error=self.status.last_error,
                )
                await self._set_status(status)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except asyncio.CancelledError:
            await self._terminate_process()
            raise
        except Exception:
            self._log.exception("worker.crashed")
            await self._set_status(
                DestinationStatus.ERROR, error="relay worker crashed (see relay logs)"
            )

    async def _run_once(self) -> None:
        cfg = self.config
        self.status.pid = None
        self.status.bitrate_kbps = 0.0
        self.status.bytes_sent = 0
        self.status.next_retry_at = None
        stderr_tail: deque[str] = deque(maxlen=30)
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                preexec_fn=_set_pdeathsig,
            )
        except (OSError, ValueError) as exc:
            self.status.consecutive_failures += 1
            await self._set_status(DestinationStatus.RETRYING, error=f"cannot start ffmpeg: {exc}")
            return
        self._proc = proc
        self.status.pid = proc.pid
        self.status.started_at = _now_iso()
        started = time.monotonic()
        self._log.info("worker.started", pid=proc.pid, cmd=redact(self.cmd, self._secrets))
        await self._set_status(DestinationStatus.STARTING, force=True)

        progress = Progress()
        samples = _Samples()
        last_progress = started
        last_emit = started
        stall_reason: str | None = None

        async def read_stdout() -> None:
            nonlocal last_progress, last_emit
            assert proc.stdout is not None
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    return
                if not progress.update(raw.decode("utf-8", "replace")):
                    continue
                now = time.monotonic()
                if progress.total_size > self.status.bytes_sent or progress.out_time_us > 0:
                    last_progress = now
                self.status.bytes_sent = progress.total_size
                self.status.bitrate_kbps = round(samples.add(now, progress.total_size), 1)
                self.status.uptime_s = round(now - started, 1)
                if self.status.status == DestinationStatus.STARTING and progress.total_size > 0:
                    self.status.consecutive_failures = (
                        self.status.consecutive_failures  # reset only after healthy_after
                    )
                    self._log.info("worker.live", pid=proc.pid)
                    await self._set_status(DestinationStatus.LIVE)
                    last_emit = now
                elif now - last_emit >= cfg.stats_interval:
                    last_emit = now
                    await self._emit(False)

        async def read_stderr() -> None:
            assert proc.stderr is not None
            while True:
                raw = await proc.stderr.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    stderr_tail.append(line)
                    self._log.debug("ffmpeg", line=line)

        async def watchdog() -> None:
            nonlocal stall_reason
            while True:
                await asyncio.sleep(1.0)
                now = time.monotonic()
                if now - last_progress > cfg.stall_timeout:
                    stall_reason = f"no output progress for {cfg.stall_timeout:.0f}s"
                    self._log.warning("worker.stalled", pid=proc.pid)
                    await self._terminate_process()
                    return

        readers = asyncio.gather(read_stdout(), read_stderr())
        wd = asyncio.create_task(watchdog())
        try:
            rc = await proc.wait()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(readers, timeout=2.0)
        finally:
            wd.cancel()
            readers.cancel()
            with contextlib.suppress(BaseException):
                await wd
            with contextlib.suppress(BaseException):
                await readers
            self._proc = None

        ran_for = time.monotonic() - started
        self.status.uptime_s = round(ran_for, 1)
        self.status.pid = None
        if self._stop.is_set():
            self._log.info("worker.stopped", rc=rc, ran_for_s=round(ran_for, 1))
            return
        if ran_for >= cfg.healthy_after:
            self.status.consecutive_failures = 1  # fresh failure after a healthy run
        else:
            self.status.consecutive_failures += 1
        tail = " | ".join(list(stderr_tail)[-5:])
        reason = stall_reason or f"ffmpeg exited with code {rc}"
        error = redact([f"{reason}: {tail}" if tail else reason], self._secrets)
        self.status.last_error = error[:2000]
        self._log.warning(
            "worker.exited", rc=rc, ran_for_s=round(ran_for, 1), error=self.status.last_error
        )

    async def _terminate_process(self) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=self.config.stop_grace)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=2.0)
