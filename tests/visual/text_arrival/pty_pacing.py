"""Run native replay in a controlling POSIX PTY; never claims GUI perception.

The PTY reader is deliberately fast. This measures application serialization,
queueing and actual file writes, not a terminal emulator's paint/compositing cost.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios
from pathlib import Path
from time import monotonic

from tests.visual.text_arrival.frame_pacing import CASES


def run(args):
    args.output.mkdir(parents=True, exist_ok=True)
    summary = args.output / f"native-{args.case}.json"
    command = [
        sys.executable,
        "-m",
        "tests.visual.text_arrival.native_pacing",
        "--case",
        args.case,
        "--theme",
        "graphite",
        "--auto-exit",
        "--output",
        str(summary),
    ]
    if args.recording:
        command.extend(["--recording", str(args.recording)])
    if args.off:
        command.append("--off")
    pid, fd = pty.fork()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 100, 0, 0))
        os.environ["TERM"] = "xterm-256color"
        os.environ["COLORTERM"] = "truecolor"
        os.execv(sys.executable, command)
    data = bytearray()
    start = monotonic()
    try:
        while monotonic() - start < args.timeout:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                data.extend(chunk)
                if b"\x1b[6n" in chunk:
                    os.write(fd, b"\x1b[1;1R")
        else:
            os.kill(pid, signal.SIGTERM)
            raise TimeoutError("native replay exceeded PTY timeout")
    finally:
        os.close(fd)
        _, status = os.waitpid(pid, 0)
        (args.output / f"native-{args.case}.ansi").write_bytes(data)
    if os.waitstatus_to_exitcode(status) != 0 or not summary.exists():
        raise RuntimeError(f"native replay failed; inspect {args.output} ANSI capture")
    print(summary, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=list(CASES), default="mixed")
    parser.add_argument("--recording", type=Path)
    parser.add_argument("--off", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=60)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
