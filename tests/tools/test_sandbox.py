"""Runtime sandbox tests: limits, isolation, and defense behind the validator."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from tests.support import CUSTOMER_1, FAST_LIMITS, plan_source

from ticketideai.tools import sandbox as sandbox_module
from ticketideai.tools.approval_queue import ApprovalQueue
from ticketideai.tools.diffing import snapshot
from ticketideai.tools.sandbox import SandboxExecutor, _strip_private_tables
from ticketideai.tools.sandbox_runner import EXIT_FORBIDDEN, SandboxLimits

GRAB_OS = (
    "def plan(api):\n"
    "    for item in ().__class__.__base__.__subclasses__():\n"
    '        if item.__name__ == "_wrap_close":\n'
    "            g = item.__init__.__globals__\n"
    "{payload}\n"
)


# Set by CPython's locale coercion and by macOS itself, never inherited from the parent.
PLATFORM_DEFAULTS = {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}


def run(source: str, url: str, limits: SandboxLimits = FAST_LIMITS):
    return SandboxExecutor(limits).execute(source, url, CUSTOMER_1)


def raw_child(payload: str, url: str, work: Path):
    """Run source straight in the child, as if the validator had been bypassed."""
    source = GRAB_OS.format(payload=f"            {payload}")
    return SandboxExecutor(FAST_LIMITS).run_child(source, url, CUSTOMER_1, work)


def test_read_only_plan_returns_result_and_no_changes(store_url: str) -> None:
    source = 'def plan(api):\n    return {"outcome": "done", "status": api.get_order(1).status}\n'
    result = run(source, store_url)
    assert result.success, result.error
    assert result.result["status"] == "delivered"
    assert result.change_set.is_empty
    assert result.diff == {"tables": {}}


def test_every_store_operation_works_under_the_audit_hook(store_url: str) -> None:
    source = plan_source(
        f'api.find_customer("{CUSTOMER_1}")',
        f'api.list_orders("{CUSTOMER_1}")',
        'api.check_stock("RIDGE-001")',
        'api.create_return(1, "RIDGE-001", 1, "damaged")',
        "api.issue_refund(1, 25.0)",
        'api.restock("RIDGE-001", 1)',
        f'api.send_customer_message("{CUSTOMER_1}", "hello")',
        'api.update_order_status(1, "returned")',
    )
    result = run(source, store_url)
    assert result.success, result.error
    kinds = [operation.kind for operation in result.change_set.operations]
    assert kinds == [
        "create_return",
        "issue_refund",
        "restock",
        "send_customer_message",
        "update_order_status",
    ]


def test_dry_run_never_writes_to_the_real_store(store_url: str, store_path: Path) -> None:
    before = snapshot(store_path)
    result = run(plan_source("api.issue_refund(1, 25.0)"), store_url)
    assert result.success, result.error
    assert result.diff["tables"]["refunds"]["added"][0]["amount"] == 25.0
    assert snapshot(store_path) == before


def test_other_customers_records_are_invisible(store_url: str) -> None:
    for line in ("api.get_order(2)", 'api.find_customer("customer2@example.com")'):
        result = run(plan_source(line), store_url)
        assert not result.success
        assert "not found" in result.error


def test_validation_failure_is_reported_without_starting_a_process(store_url: str) -> None:
    result = run("import os\ndef plan(api):\n    return 1\n", store_url)
    assert result.error_kind == "validation"


@pytest.mark.parametrize(
    ("name", "lines", "kinds"),
    [
        ("infinite_loop", ["for i in range(1000000000):", "    x = i"], {"cpu", "timeout"}),
        (
            "nested_loops",
            ["for a in range(100000):", "    for b in range(100000):", "        x = a"],
            {"cpu", "timeout"},
        ),
        ("big_list", ["x = [0] * 1000000000"], {"memory", "runtime"}),
        ("big_string", ['x = "a" * 1000000000'], {"memory", "runtime"}),
        (
            "doubling_list",
            ["x = [0]", "for i in range(60):", "    x = x + x"],
            {"memory", "runtime"},
        ),
        (
            "squaring_int",
            ["x = 3", "for i in range(64):", "    x = x * x"],
            {"memory", "cpu", "timeout", "runtime"},
        ),
    ],
)
def test_resource_bombs_are_stopped(
    store_url: str, name: str, lines: list[str], kinds: set[str]
) -> None:
    result = run(plan_source(*lines), store_url)
    assert not result.success, name
    assert result.error_kind in kinds, result.error


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS has no address-space limit")
def test_brief_memory_spike_is_caught_from_the_kernels_peak_record(
    store_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plan can allocate and exit between watchdog polls, so the peak is checked on exit."""
    monkeypatch.setattr(sandbox_module, "rss_kb", lambda _pid: 0)
    result = run(plan_source('x = "a" * 500000000', "x = 1"), store_url)
    assert not result.success
    assert result.error_kind == "memory"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux enforces the address-space limit")
def test_large_allocation_is_stopped_without_the_watchdog(
    store_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox_module, "rss_kb", lambda _pid: 0)
    result = run(plan_source('x = "a" * 600000000', 'y = "b" * 600000000'), store_url)
    assert not result.success
    assert "MemoryError" in result.error


def test_a_parents_memory_use_is_not_charged_to_the_plan(store_url: str) -> None:
    ballast = bytearray(400 * 1024 * 1024)
    ballast[::4096] = b"x" * len(ballast[::4096])
    result = run(plan_source('api.check_stock("RIDGE-001")'), store_url)
    assert result.success, result.error


def test_deep_nesting_fails_cleanly(store_url: str) -> None:
    source = (
        "def plan(api):\n    x = []\n    for i in range(200000):\n        x = [x]\n"
        '    return {"summary": x}\n'
    )
    assert not run(source, store_url).success


def test_output_flood_is_stopped(store_url: str) -> None:
    source = 'def plan(api):\n    return {"summary": "x" * 5000000}\n'
    result = run(source, store_url)
    assert result.error_kind == "output"


def test_operation_flood_is_stopped(store_url: str) -> None:
    result = run(
        plan_source("for i in range(100000):", '    api.restock("RIDGE-001", 1)'), store_url
    )
    assert not result.success
    assert "limit" in result.error


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("os_system", 'g["system"]("touch CANARY")'),
        ("os_open", 'g["open"]("CANARY", 577)'),
        ("listdir", 'g["listdir"](".")'),
        ("fork", 'g["fork"]()'),
        ("remove", 'g["remove"]("anything")'),
        ("exec_builtin", 'g["__builtins__"]["exec"]("1")'),
        ("compile_builtin", 'g["__builtins__"]["compile"]("1", "x", "eval")'),
        ("open_builtin", 'g["__builtins__"]["open"]("CANARY", "w")'),
        ("import_new_module", 'g["__builtins__"]["__import__"]("antigravity")'),
    ],
)
def test_audit_hook_stops_escapes_that_pass_the_validator(
    store_url: str, tmp_path: Path, name: str, payload: str
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    exit_info = raw_child(payload, store_url, work)
    assert exit_info.returncode == EXIT_FORBIDDEN, (name, exit_info.stderr)
    assert not (work / "CANARY").exists()


def test_child_cannot_read_the_parent_environment(
    store_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-leak")
    exit_info = raw_child('return {"env": dict(g["environ"])}', store_url, tmp_path)
    frame = json.loads(exit_info.stdout)
    assert set(frame["result"]["env"]) <= PLATFORM_DEFAULTS
    assert b"sk-must-not-leak" not in exit_info.stdout


def test_child_runs_in_its_own_working_directory(store_url: str, tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    exit_info = raw_child('return {"cwd": g["getcwd"]()}', store_url, work)
    assert json.loads(exit_info.stdout)["result"]["cwd"] == os.path.realpath(work)


def test_command_is_isolated_with_an_empty_environment() -> None:
    executor = SandboxExecutor()
    assert executor.command()[1] == "-I"
    assert executor.child_env() == {}


def test_child_copy_does_not_contain_the_approval_queue(store_url: str, store_path: Path) -> None:
    ApprovalQueue(store_url).initialize()
    _strip_private_tables(store_path)
    with closing(sqlite3.connect(store_path)) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    assert "approval_queue" not in tables


def test_limits_are_applied_by_the_kernel(tmp_path: Path) -> None:
    probe = (
        "import json, resource\n"
        "from ticketideai.tools.sandbox_runner import SandboxLimits, apply_limits\n"
        "apply_limits(SandboxLimits(cpu_seconds=3, file_size_mb=2, open_files=40))\n"
        "names = ['RLIMIT_CPU', 'RLIMIT_FSIZE', 'RLIMIT_NOFILE', 'RLIMIT_CORE']\n"
        "print(json.dumps({n: resource.getrlimit(getattr(resource, n))[0] for n in names}))\n"
    )
    output = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, cwd=tmp_path
    ).stdout
    limits = json.loads(output)
    assert 3 <= limits["RLIMIT_CPU"] <= 6
    assert limits["RLIMIT_FSIZE"] == 2 * 1024 * 1024
    assert limits["RLIMIT_NOFILE"] == 40
    assert limits["RLIMIT_CORE"] == 0
