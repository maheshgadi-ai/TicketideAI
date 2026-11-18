"""Enforce the layering rule: imports inside `ticketideai` only flow downward."""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "ticketideai"

# Higher layers may import lower ones; same-rank packages may not import each other.
LAYER = {
    "config": 0,
    "schemas": 0,
    "prompts": 0,
    "observability": 1,
    "llm": 2,
    "tools": 2,
    "agents": 3,
    "orchestrator": 4,
    "evals": 5,  # runs the orchestrator over a dataset
    "interfaces": 6,
}


def _package(module: str) -> str | None:
    parts = module.split(".")
    return parts[1] if len(parts) > 1 and parts[0] == "ticketideai" else None


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def _violations() -> list[str]:
    bad = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC)
        if len(rel.parts) < 2:
            continue  # top-level ticketideai/__init__.py
        src_pkg = rel.parts[0]
        assert src_pkg in LAYER, f"Add package '{src_pkg}' to LAYER in this test"
        for module in _imports(path):
            target = _package(module)
            if target is None or target == src_pkg or target == "__init__":
                continue
            if target in LAYER and LAYER[target] >= LAYER[src_pkg]:
                bad.append(f"{rel} imports {module}")
    return bad


def test_imports_only_flow_downward() -> None:
    assert _violations() == []


def test_tools_never_import_llm() -> None:
    for path in (SRC / "tools").rglob("*.py"):
        assert not any(m.startswith("ticketideai.llm") for m in _imports(path)), path


def test_agents_never_import_each_other() -> None:
    """Only the orchestrator wires agents together; agents may share `agents.base` only.

    The package's `__init__.py` only re-exports, so it is exempt.
    """
    for path in (SRC / "agents").rglob("*.py"):
        if path.name == "__init__.py":
            continue
        siblings = {
            m
            for m in _imports(path)
            if m.startswith("ticketideai.agents.") and m != "ticketideai.agents.base"
        }
        assert not siblings, f"{path.name} imports {siblings}"


def test_modules_stay_small() -> None:
    long = [
        str(p.relative_to(SRC)) for p in SRC.rglob("*.py") if len(p.read_text().splitlines()) > 300
    ]
    assert long == []


def test_model_code_is_only_executed_by_the_sandbox_runner() -> None:
    """`exec` and `eval` may appear in exactly one module: the sandboxed child."""
    users = set()
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"exec", "eval"}
            ):
                users.add(str(path.relative_to(SRC)))
    assert users == {"tools/sandbox_runner.py"}


def test_store_writes_only_happen_in_the_effects_module() -> None:
    """Row classes are only constructed for writing in `tools/effects.py` and the seed."""
    writers = set()
    names = {"ReturnRow", "RefundRow", "RestockRow", "MessageRow"}
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) in names:
                writers.add(str(path.relative_to(SRC)))
    assert writers == {"tools/effects.py", "tools/seed_store.py"}
