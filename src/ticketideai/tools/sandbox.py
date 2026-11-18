"""Run validated plans in an isolated, resource-limited child process."""

from __future__ import annotations

import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from ticketideai.schemas.operations import ChangeSet
from ticketideai.tools.database import sqlite_path, sqlite_url
from ticketideai.tools.diffing import snapshot, table_diff
from ticketideai.tools.effects import commit_change_set
from ticketideai.tools.errors import StoreError
from ticketideai.tools.sandbox_process import (
    CappedReader,
    exceeded_peak_memory,
    kill_group,
    reap,
    rss_kb,
    send_job,
)
from ticketideai.tools.sandbox_runner import (
    EXIT_FORBIDDEN,
    EXIT_LIMITS_UNAVAILABLE,
    SandboxLimits,
    ensure_not_root,
    job_limits,
)
from ticketideai.tools.validator import PlanValidator

ErrorKind = Literal[
    "validation", "runtime", "timeout", "cpu", "memory", "output", "forbidden", "verification"
]
_POLL_SECONDS = 0.05
_BOOTSTRAP = (
    "import sys; sys.path.insert(0, sys.argv[1]); "
    "from ticketideai.tools.sandbox_runner import main; main()"
)


@dataclass(frozen=True)
class SandboxResult:
    """The outcome of one dry run. The diff comes from trusted replay, never the child."""

    success: bool
    result: dict[str, Any] = field(default_factory=dict)
    change_set: ChangeSet = field(default_factory=ChangeSet)
    diff: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    error_kind: ErrorKind | None = None

    @property
    def timed_out(self) -> bool:
        """True when the plan hit the wall-clock limit."""
        return self.error_kind == "timeout"


@dataclass(frozen=True)
class ChildExit:
    """Raw result of the child process."""

    stdout: bytes
    stderr: bytes
    returncode: int | None
    killed_for: ErrorKind | None = None


class _Frame(BaseModel):
    ok: bool
    error: str = ""
    result: dict[str, Any] = {}
    change_set: ChangeSet = ChangeSet()


class SandboxExecutor:
    """Validate a plan, dry-run it on a temporary copy, and verify what it recorded."""

    def __init__(self, limits: SandboxLimits | None = None) -> None:
        self.limits = limits or SandboxLimits()
        self.validator = PlanValidator()

    def execute(self, source: str, database_url: str, customer_email: str) -> SandboxResult:
        """Dry-run `source` for one customer; the real database is never written."""
        validation = self.validator.validate(source)
        if not validation.valid:
            return _failure("validation", "; ".join(validation.errors))
        database = sqlite_path(database_url)
        if not database.exists():
            raise FileNotFoundError(f"SQLite database not found: {database}")
        with tempfile.TemporaryDirectory(prefix="ticketide-sandbox-") as work:
            return self._dry_run(source, database, customer_email, Path(work))

    def command(self) -> list[str]:
        """Return the isolated interpreter command used for the child."""
        source_root = str(Path(__file__).resolve().parents[2])
        return [sys.executable, "-I", "-X", "utf8", "-c", _BOOTSTRAP, source_root]

    @staticmethod
    def child_env() -> dict[str, str]:
        """Return the child's environment, which is deliberately empty."""
        return {}

    def run_child(
        self, source: str, database_url: str, customer_email: str, work_dir: Path
    ) -> ChildExit:
        """Start the child, enforce the limits from outside, and collect its output."""
        ensure_not_root()
        job = {
            "source": source,
            "database_url": sqlite_url(database_url),
            "customer_email": customer_email,
            "limits": job_limits(self.limits),
        }
        process = subprocess.Popen(
            self.command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=work_dir,
            env=self.child_env(),
            start_new_session=True,
            close_fds=True,
        )
        stdout, stderr = CappedReader(process.stdout), CappedReader(process.stderr)
        for reader in (stdout, stderr):
            reader.start(self.limits.output_bytes)
        threading.Thread(target=send_job, args=(process, job), daemon=True).start()
        killed_for = self._supervise(process, stdout, stderr)
        stdout.join()
        stderr.join()
        return ChildExit(stdout.data(), stderr.data(), process.returncode, killed_for)

    def _dry_run(
        self, source: str, database: Path, customer_email: str, work: Path
    ) -> SandboxResult:
        child_dir = work / "child"
        child_dir.mkdir(mode=0o700)
        child_db = child_dir / "store.sqlite"
        pristine = work / "pristine.sqlite"
        _copy_database(database, pristine)
        _copy_database(database, child_db)
        _strip_private_tables(child_db)
        exit_info = self.run_child(source, f"sqlite:///{child_db}", customer_email, child_dir)
        failure = _exit_failure(exit_info)
        if failure is not None:
            return failure
        try:
            frame = _Frame.model_validate_json(exit_info.stdout)
        except ValidationError:
            return _failure("runtime", "plan process did not return a valid result")
        if not frame.ok:
            return _failure("runtime", frame.error)
        return _verified(frame, pristine)

    def _supervise(
        self, process: subprocess.Popen[bytes], stdout: CappedReader, stderr: CappedReader
    ) -> ErrorKind | None:
        deadline = time.monotonic() + self.limits.wall_seconds
        rss_limit_kb = self.limits.rss_mb * 1024
        while (usage := reap(process)) is None:
            reason: ErrorKind | None = None
            if time.monotonic() > deadline:
                reason = "timeout"
            elif stdout.overflowed or stderr.overflowed:
                reason = "output"
            elif rss_kb(process.pid) > rss_limit_kb:
                reason = "memory"
            if reason is not None:
                kill_group(process)
                return reason
            time.sleep(_POLL_SECONDS)
        if stdout.overflowed or stderr.overflowed:
            return "output"
        return "memory" if exceeded_peak_memory(usage, rss_limit_kb) else None


def _exit_failure(exit_info: ChildExit) -> SandboxResult | None:
    messages: dict[ErrorKind, str] = {
        "timeout": "plan exceeded the time limit",
        "memory": "plan exceeded the memory limit",
        "output": "plan exceeded the output limit",
    }
    if exit_info.killed_for is not None:
        return _failure(exit_info.killed_for, messages[exit_info.killed_for])
    code = exit_info.returncode
    if code == EXIT_FORBIDDEN:
        detail = exit_info.stderr.decode("utf-8", "replace").strip()
        return _failure("forbidden", detail or "plan attempted a forbidden operation")
    if code == EXIT_LIMITS_UNAVAILABLE:
        return _failure("forbidden", "sandbox resource limits could not be applied")
    if code == -signal.SIGXCPU or code == -signal.SIGKILL:
        return _failure("cpu", "plan exceeded the CPU limit")
    if code != 0:
        return _failure("runtime", f"plan process exited with status {code}")
    return None


def _verified(frame: _Frame, pristine: Path) -> SandboxResult:
    """Replay the reported change set on an untouched copy with the commit code path."""
    before = snapshot(pristine)
    try:
        commit_change_set(f"sqlite:///{pristine}", frame.change_set)
    except (StoreError, SQLAlchemyError) as exc:
        return _failure("verification", f"change set failed verification: {exc}")
    diff = table_diff(before, snapshot(pristine))
    return SandboxResult(True, result=frame.result, change_set=frame.change_set, diff=diff)


def _copy_database(source: Path, target: Path) -> None:
    with closing(sqlite3.connect(source)) as origin, closing(sqlite3.connect(target)) as copy:
        origin.backup(copy)


def _strip_private_tables(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("DROP TABLE IF EXISTS approval_queue")
        connection.commit()


def _failure(kind: ErrorKind, message: str) -> SandboxResult:
    return SandboxResult(False, error=message, error_kind=kind)
