"""Process helpers the sandbox parent uses to supervise a plan from outside."""

from __future__ import annotations

import json
import os
import resource
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any


class CappedReader:
    """Drain a pipe on a thread, keeping only the first `limit` bytes."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._chunks: list[bytes] = []
        self._size = 0
        self._limit = 0
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self.overflowed = False

    def start(self, limit: int) -> None:
        self._limit = limit
        self._thread.start()

    def join(self) -> None:
        self._thread.join(timeout=2.0)

    def data(self) -> bytes:
        return b"".join(self._chunks)

    def _drain(self) -> None:
        while chunk := self._stream.read(65536):
            self._size += len(chunk)
            if self._size > self._limit:
                self.overflowed = True
            else:
                self._chunks.append(chunk)


def send_job(process: subprocess.Popen[bytes], job: dict[str, Any]) -> None:
    try:
        process.stdin.write(json.dumps(job).encode("utf-8"))
        process.stdin.close()
    except (BrokenPipeError, OSError, ValueError):
        return


def kill_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()
    process.wait()


def reap(process: subprocess.Popen[bytes]) -> resource.struct_rusage | None:
    """Collect a finished child and the kernel's record of its resource use."""
    pid, status, usage = os.wait4(process.pid, os.WNOHANG)
    if pid == 0:
        return None
    process.returncode = os.waitstatus_to_exitcode(status)
    return usage


def exceeded_peak_memory(usage: resource.struct_rusage, limit_kb: int) -> bool:
    """Check the kernel's peak-memory record on macOS, which lacks an address-space limit.

    A plan can allocate and exit between watchdog polls. Linux stops large allocations with
    `RLIMIT_AS`, and its peak record is unusable here because it carries over the parent's
    memory use across `exec`.
    """
    return sys.platform == "darwin" and usage.ru_maxrss // 1024 > limit_kb


def rss_kb(pid: int) -> int:
    """Return a process's resident memory in KiB, or 0 when it cannot be read."""
    statm = Path(f"/proc/{pid}/statm")
    try:
        if statm.exists():
            return int(statm.read_text().split()[1]) * (os.sysconf("SC_PAGE_SIZE") // 1024)
        output = subprocess.run(
            ["/bin/ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        ).stdout
        return int(output.strip() or 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0
