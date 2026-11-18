"""Child-process entry point that dry-runs one validated plan under OS limits."""

from __future__ import annotations

import builtins
import json
import math
import os
import resource
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy.orm import Session

from ticketideai.schemas.operations import (
    CreateReturn,
    IssueRefund,
    MessageFacts,
    Restock,
    SendCustomerMessage,
    UpdateOrderStatus,
)
from ticketideai.schemas.store import Product, RefundRecord, RestockRecord, ReturnRecord
from ticketideai.tools.database import OrderRow, ProductRow
from ticketideai.tools.effects import apply_operation, order_facts, restock_facts, return_facts
from ticketideai.tools.errors import SandboxConfigurationError
from ticketideai.tools.store_api import ChangeRecorder, StoreAPI
from ticketideai.tools.validator import ALLOWED_BUILTINS, API_METHODS

EXIT_FORBIDDEN = 70
EXIT_LIMITS_UNAVAILABLE = 71
MAX_ERROR_CHARS = 2000
_DENIED_EVENTS = frozenset(
    {"compile", "import", "open", "code.__new__", "marshal.loads", "pickle.find_class"}
)
_DENIED_PREFIXES = (
    "cpython.run_",
    "ctypes.",
    "ftplib.",
    "gc.get_",
    "glob.",
    "http.",
    "imaplib.",
    "mmap.",
    "nntplib.",
    "os.",
    "pathlib.",
    "poplib.",
    "pty.",
    "resource.",
    "shutil.",
    "smtplib.",
    "socket.",
    "subprocess.",
    "sys.addaudithook",
    "sys.setprofile",
    "sys.settrace",
    "sys.unraisablehook",
    "telnetlib.",
    "tempfile.",
    "urllib.",
    "webbrowser.",
)


@dataclass(frozen=True)
class SandboxLimits:
    """Resource ceilings applied to each plan process."""

    wall_seconds: float = 10.0
    cpu_seconds: int = 5
    address_space_mb: int = 1024
    rss_mb: int = 256
    file_size_mb: int = 16
    open_files: int = 64
    processes: int = 0
    output_bytes: int = 1_000_000


class PlanAPI:
    """The nine store operations, and nothing else, handed to the plan."""

    __slots__ = tuple(sorted(API_METHODS))

    def __init__(self, store: StoreAPI) -> None:
        for name in API_METHODS:
            setattr(self, name, getattr(store, name))


def ensure_not_root() -> None:
    """Refuse to run as root, which the kernel exempts from `RLIMIT_NPROC`."""
    if os.geteuid() == 0:
        raise SandboxConfigurationError(
            "the sandbox must not run as root: the process limit does not apply to root"
        )


def required_limits(platform: str = sys.platform) -> frozenset[str]:
    """Return the limits that must be in force before plan code may start.

    macOS does not support `RLIMIT_AS` and treats `RLIMIT_NPROC` differently, so there the
    parent's memory watchdog and the audit hook carry those two guarantees.
    """
    required = {"RLIMIT_CORE", "RLIMIT_CPU", "RLIMIT_FSIZE", "RLIMIT_NOFILE"}
    return frozenset(required | {"RLIMIT_AS", "RLIMIT_NPROC"} if platform == "linux" else required)


def apply_limits(limits: SandboxLimits) -> dict[str, int]:
    """Apply kernel resource limits and return the ones the platform accepted."""
    used_cpu = math.ceil(time.process_time())
    megabyte = 1024 * 1024
    wanted = {
        "RLIMIT_CORE": (0, 0),
        "RLIMIT_CPU": (used_cpu + limits.cpu_seconds, used_cpu + limits.cpu_seconds + 1),
        "RLIMIT_AS": (limits.address_space_mb * megabyte,) * 2,
        "RLIMIT_FSIZE": (limits.file_size_mb * megabyte,) * 2,
        "RLIMIT_NOFILE": (limits.open_files,) * 2,
        "RLIMIT_NPROC": (limits.processes,) * 2,
    }
    applied: dict[str, int] = {}
    for name, values in wanted.items():
        try:
            resource.setrlimit(getattr(resource, name), values)
            applied[name] = values[0]
        except (AttributeError, OSError, ValueError):
            continue
    return applied


def install_audit_hook() -> Callable[[], None]:
    """Terminate the process on any file, network, process, or code-loading event.

    Returns a function that arms the hook; one `exec` is allowed afterwards so the
    validated plan module can define `plan`.
    """
    state = {"armed": False, "exec_allowed": 1}
    write, leave = os.write, os._exit

    def hook(event: str, _args: tuple[Any, ...]) -> None:
        if not state["armed"]:
            return
        if event == "exec" and state["exec_allowed"]:
            state["exec_allowed"] -= 1
            return
        if event == "exec" or event in _DENIED_EVENTS or event.startswith(_DENIED_PREFIXES):
            write(2, f"forbidden operation: {event}\n".encode())
            leave(EXIT_FORBIDDEN)

    sys.addaudithook(hook)

    def arm() -> None:
        state["armed"] = True

    return arm


def main() -> None:
    """Read one job from stdin, run the plan, and write one JSON frame to stdout."""
    ensure_not_root()
    job = json.loads(sys.stdin.buffer.read())
    recorder = ChangeRecorder()
    store = StoreAPI(job["database_url"], scope_email=job["customer_email"], recorder=recorder)
    _warm_up(store)
    code = compile(job["source"], "<plan>", "exec")
    api = PlanAPI(store)
    namespace: dict[str, Any] = {
        "__builtins__": {n: getattr(builtins, n) for n in ALLOWED_BUILTINS}
    }
    arm = install_audit_hook()
    if required_limits() - apply_limits(SandboxLimits(**job["limits"])).keys():
        os._exit(EXIT_LIMITS_UNAVAILABLE)
    arm()
    _emit(_run_plan(code, namespace, api, recorder))
    os._exit(0)  # skip interpreter shutdown, which would import modules under the armed hook


def job_limits(limits: SandboxLimits) -> dict[str, Any]:
    """Serialize limits for the child's job description."""
    return asdict(limits)


def _run_plan(
    code: Any, namespace: dict[str, Any], api: PlanAPI, recorder: ChangeRecorder
) -> dict[str, Any]:
    try:
        exec(code, namespace)
        value = namespace["plan"](api)
        result = value if isinstance(value, dict) else {"summary": str(value)}
        return {
            "ok": True,
            "result": json.loads(json.dumps(result, default=str)),
            "change_set": recorder.change_set().model_dump(mode="json"),
        }
    except BaseException as exc:
        message = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
        return {"ok": False, "error": message}


def _emit(frame: dict[str, Any]) -> None:
    payload = json.dumps(frame, ensure_ascii=False).encode("utf-8")
    while payload:
        payload = payload[os.write(1, payload) :]


def _warm_up(store: StoreAPI) -> None:
    """Exercise every code path once so no lazy import happens after the hook is armed."""
    with Session(store.engine) as session:
        order = session.query(OrderRow).first()
        if order is not None and order.lines:
            sku = order.lines[0].sku
            facts = order_facts(session, order.id)
            operations = (
                CreateReturn(
                    order_id=order.id,
                    sku=sku,
                    quantity=1,
                    reason="warm-up",
                    facts=return_facts(session, order.id, sku),
                ),
                IssueRefund(order_id=order.id, amount_cents=1, facts=facts),
                Restock(sku=sku, quantity=1, facts=restock_facts(session, sku)),
                UpdateOrderStatus(order_id=order.id, new_status=order.status, facts=facts),
                SendCustomerMessage(
                    customer_email="warm-up@example.com",
                    body="warm-up",
                    facts=MessageFacts(customer_id=order.customer_id),
                ),
            )
            rows = [apply_operation(session, operation) for operation in operations]
            ReturnRecord.model_validate(rows[0])
            RefundRecord.model_validate(rows[1])
            RestockRecord.model_validate(rows[2])
            Product.model_validate(session.get(ProductRow, sku))
            StoreAPI._order(order).model_dump(mode="json")
        session.rollback()
