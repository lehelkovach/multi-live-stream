#!/usr/bin/env python3
"""Stand-in for ffmpeg in worker tests. Behaviour is selected with ``--mode``:

ok          print progress blocks forever (until SIGTERM)
fail        write an error to stderr and exit 1 immediately
exit-after  run normally for --seconds then exit 1 (simulates a dropped connection)
stall       print one progress block, then stay silent forever
slow        sleep --seconds before the first progress block (tests "starting")
"""

from __future__ import annotations

import argparse
import signal
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="ok")
    parser.add_argument("--seconds", type=float, default=1.0)
    parser.add_argument("--interval", type=float, default=0.2)
    args, _ignored = parser.parse_known_args()  # tolerate ffmpeg-style args

    def on_term(_sig: int, _frame: object) -> None:
        print("fake-ffmpeg: terminated", file=sys.stderr, flush=True)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, on_term)

    if args.mode == "fail":
        print("[rtmp @ 0x1] Cannot open connection tcp://example:1935", file=sys.stderr)
        print("Error opening output file rtmp://example/live/key.", file=sys.stderr, flush=True)
        return 1

    if args.mode == "slow":
        time.sleep(args.seconds)

    start = time.monotonic()
    total = 0
    frame = 0
    while True:
        frame += 6
        total += 60_000
        out_time = int((time.monotonic() - start) * 1_000_000)
        sys.stdout.write(
            f"frame={frame}\nfps=30.0\nbitrate=2400.0kbits/s\ntotal_size={total}\n"
            f"out_time_us={out_time}\nout_time_ms={out_time}\nspeed=1.0x\nprogress=continue\n"
        )
        sys.stdout.flush()
        if args.mode == "stall":
            signal.pause()
        if args.mode == "exit-after" and time.monotonic() - start >= args.seconds:
            print("av_interleaved_write_frame(): Broken pipe", file=sys.stderr, flush=True)
            return 1
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
