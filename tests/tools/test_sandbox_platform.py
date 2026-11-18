"""Root refusal, required limits, and kernel enforcement of the sandbox limits."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from tests.support import CUSTOMER_1, FAST_LIMITS, plan_source

from ticketideai.tools.errors import SandboxConfigurationError
from ticketideai.tools.sandbox import SandboxExecutor
from ticketideai.tools.sandbox_runner import ensure_not_root, required_limits

linux_only = pytest.mark.skipif(sys.platform != "linux", reason="Linux kernel limits")
PRELUDE = (
    "import errno, json, os, subprocess, sys\n"
    "from ticketideai.tools.sandbox_runner import SandboxLimits, apply_limits\n"
    "applied = apply_limits(SandboxLimits({limits}))\n"
)


def probe(limits: str, body: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run code in a fresh interpreter after applying only the sandbox's kernel limits."""
    script = PRELUDE.format(limits=limits) + body
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd=cwd, timeout=30
    )


def test_root_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    with pytest.raises(SandboxConfigurationError, match="must not run as root"):
        ensure_not_root()


def test_sandbox_does_not_start_a_child_as_root(
    store_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    started: list[object] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kw: started.append(args))
    with pytest.raises(SandboxConfigurationError):
        SandboxExecutor(FAST_LIMITS).execute(plan_source("x = 1"), store_url, CUSTOMER_1)
    assert started == []


def test_linux_requires_every_limit() -> None:
    assert {"RLIMIT_AS", "RLIMIT_NPROC", "RLIMIT_CPU", "RLIMIT_FSIZE"} <= required_limits("linux")
    assert "RLIMIT_AS" not in required_limits("darwin")


@linux_only
def test_every_required_limit_is_accepted_by_the_kernel(tmp_path: Path) -> None:
    result = probe("", "print(json.dumps(sorted(applied)))", tmp_path)
    assert required_limits() <= set(json.loads(result.stdout)), result.stderr


@linux_only
def test_kernel_stops_cpu_spin(tmp_path: Path) -> None:
    result = probe("cpu_seconds=1", "while True:\n    pass\n", tmp_path)
    assert result.returncode in {-signal.SIGXCPU, -signal.SIGKILL}


@linux_only
def test_kernel_stops_large_allocation(tmp_path: Path) -> None:
    body = (
        "try:\n"
        "    data = bytearray(2 * 1024 * 1024 * 1024)\n"
        "except MemoryError:\n"
        "    print('blocked')\n"
    )
    result = probe("address_space_mb=512", body, tmp_path)
    assert result.stdout.strip() == "blocked", result.stderr


@linux_only
def test_kernel_stops_large_files(tmp_path: Path) -> None:
    body = (
        "try:\n"
        "    with open('big.bin', 'wb', buffering=0) as handle:\n"
        "        handle.write(b'x' * (2 * 1024 * 1024))\n"
        "        handle.write(b'x' * (2 * 1024 * 1024))\n"
        "except OSError as exc:\n"
        "    print(errno.errorcode[exc.errno])\n"
    )
    result = probe("file_size_mb=1", body, tmp_path)
    assert result.stdout.strip() == "EFBIG", result.stderr
    assert (tmp_path / "big.bin").stat().st_size <= 1024 * 1024


@linux_only
def test_kernel_stops_child_processes(tmp_path: Path) -> None:
    body = (
        "try:\n"
        "    pid = os.fork()\n"
        "    if pid == 0:\n"
        "        os._exit(0)\n"
        "    print('forked')\n"
        "except OSError as exc:\n"
        "    print(errno.errorcode[exc.errno])\n"
    )
    result = probe("processes=0", body, tmp_path)
    assert result.stdout.strip() == "EAGAIN", (result.stdout, result.stderr)


@linux_only
def test_kernel_stops_extra_open_files(tmp_path: Path) -> None:
    body = (
        "handles = []\n"
        "try:\n"
        "    for index in range(100):\n"
        "        handles.append(open(os.devnull))\n"
        "except OSError as exc:\n"
        "    print(errno.errorcode[exc.errno])\n"
    )
    result = probe("open_files=32", body, tmp_path)
    assert result.stdout.strip() == "EMFILE", result.stderr
