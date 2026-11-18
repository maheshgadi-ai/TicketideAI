"""Strict AST allowlist for model-written `plan(api)` source.

Everything is denied unless listed here: node types, names, attributes, and calls.
"""

from __future__ import annotations

import ast
import io
import tokenize
from dataclasses import dataclass

MAX_SOURCE_CHARS = 20_000
MAX_AST_NODES = 3_000
MAX_INT_LITERAL = 1_000_000_000
MAX_STRING_LITERAL = 4_000

API_METHODS = frozenset(
    {
        "find_customer",
        "get_order",
        "list_orders",
        "check_stock",
        "create_return",
        "issue_refund",
        "restock",
        "update_order_status",
        "send_customer_message",
    }
)
ALLOWED_BUILTINS = frozenset(
    {
        "abs",
        "all",
        "any",
        "bool",
        "dict",
        "enumerate",
        "float",
        "int",
        "len",
        "list",
        "max",
        "min",
        "range",
        "round",
        "set",
        "sorted",
        "str",
        "sum",
        "tuple",
        "zip",
    }
)
DATA_ATTRIBUTES = frozenset(
    {
        "amount",
        "category",
        "customer_id",
        "email",
        "id",
        "lines",
        "name",
        "note",
        "order_id",
        "ordered_at",
        "price",
        "quantity",
        "reason",
        "sku",
        "status",
        "stock",
        "stock_after",
        "unit_price",
    }
)
SAFE_METHODS = frozenset(
    {
        "append",
        "endswith",
        "get",
        "isoformat",
        "items",
        "join",
        "keys",
        "lower",
        "split",
        "startswith",
        "strip",
        "upper",
        "values",
    }
)

_ALLOWED_NODES = (
    ast.Module,
    ast.FunctionDef,
    ast.arguments,
    ast.arg,
    ast.Assign,
    ast.AugAssign,
    ast.Return,
    ast.Expr,
    ast.If,
    ast.For,
    ast.Break,
    ast.Continue,
    ast.Pass,
    ast.Call,
    ast.keyword,
    ast.Name,
    ast.Attribute,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Set,
    ast.Subscript,
    ast.Slice,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.JoinedStr,
    ast.FormattedValue,
    ast.Load,
    ast.Store,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.UAdd,
    ast.USub,
    ast.Not,
    ast.And,
    ast.Or,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Is,
    ast.IsNot,
    ast.In,
    ast.NotIn,
)


class PlanValidationError(ValueError):
    """Raised when plan source violates the sandbox allowlist."""


@dataclass(frozen=True)
class ValidationResult:
    """Whether a plan passed, and every reason it did not."""

    valid: bool
    errors: tuple[str, ...] = ()


class PlanValidator:
    """Validate a `plan(api)` function without executing it."""

    def validate(self, source: str) -> ValidationResult:
        """Return every allowlist violation found in `source`."""
        if not source or not source.strip():
            return ValidationResult(False, ("plan source is empty",))
        if len(source) > MAX_SOURCE_CHARS:
            return ValidationResult(False, (f"plan source exceeds {MAX_SOURCE_CHARS} characters",))
        try:
            tree = ast.parse(source, mode="exec")
        except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
            return ValidationResult(False, (f"plan source could not be parsed: {exc}",))
        errors = _identifier_errors(source)
        nodes = list(ast.walk(tree))
        if len(nodes) > MAX_AST_NODES:
            errors.append(f"plan exceeds {MAX_AST_NODES} syntax nodes")
        errors += _structure_errors(tree)
        errors += _node_errors(nodes)
        return ValidationResult(not errors, tuple(dict.fromkeys(errors)))

    def validate_or_raise(self, source: str) -> None:
        """Raise `PlanValidationError` when `source` is not allowed."""
        result = self.validate(source)
        if not result.valid:
            raise PlanValidationError("; ".join(result.errors))


def _identifier_errors(source: str) -> list[str]:
    """Reject non-ASCII identifiers, which Python would normalize into other names."""
    errors: list[str] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.NAME and not token.string.isascii():
                errors.append(f"non-ASCII identifier: {token.string!r}")
    except (tokenize.TokenError, SyntaxError, IndentationError) as exc:
        errors.append(f"plan source could not be tokenized: {exc}")
    return errors


def _structure_errors(tree: ast.Module) -> list[str]:
    body = tree.body
    plan = body[0] if len(body) == 1 else None
    if not isinstance(plan, ast.FunctionDef) or plan.name != "plan":
        return ["source must contain exactly one top-level plan(api) function"]
    errors: list[str] = []
    arguments = plan.args
    extras = (
        arguments.posonlyargs,
        arguments.kwonlyargs,
        arguments.defaults,
        arguments.kw_defaults,
        arguments.vararg,
        arguments.kwarg,
    )
    if [arg.arg for arg in arguments.args] != ["api"] or any(extras):
        errors.append("plan function must accept exactly one argument: api")
    if plan.decorator_list or plan.returns or any(arg.annotation for arg in arguments.args):
        errors.append("plan function must not use decorators or annotations")
    return errors


def _node_errors(nodes: list[ast.AST]) -> list[str]:
    called = {id(node.func) for node in nodes if isinstance(node, ast.Call)}
    assigned = {
        node.id for node in nodes if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    }
    errors: list[str] = []
    function_count = 0
    for node in nodes:
        function_count += isinstance(node, ast.FunctionDef)
        if not isinstance(node, _ALLOWED_NODES):
            errors.append(f"forbidden node: {type(node).__name__}")
        elif isinstance(node, ast.Attribute):
            errors += _attribute_errors(node, id(node) in called)
        elif isinstance(node, ast.Name):
            errors += _name_errors(node, assigned)
        elif isinstance(node, ast.Call):
            errors += _call_errors(node)
        elif isinstance(node, ast.keyword) and node.arg is None:
            errors.append("forbidden keyword unpacking")
        elif isinstance(node, ast.Constant):
            errors += _constant_errors(node)
    if function_count > 1:
        errors.append("nested functions are not allowed")
    return errors


def _attribute_errors(node: ast.Attribute, is_called: bool) -> list[str]:
    attribute = node.attr
    if attribute.startswith("_") or not isinstance(node.ctx, ast.Load):
        return [f"forbidden attribute: {attribute}"]
    if isinstance(node.value, ast.Name) and node.value.id == "api":
        if attribute in API_METHODS and is_called:
            return []
        return [f"forbidden API call: {attribute}"]
    allowed = SAFE_METHODS if is_called else DATA_ATTRIBUTES
    return [] if attribute in allowed else [f"forbidden attribute: {attribute}"]


def _name_errors(node: ast.Name, assigned: set[str]) -> list[str]:
    name = node.id
    if name == "_":
        return []
    if name.startswith("_"):
        return [f"forbidden name: {name}"]
    if isinstance(node.ctx, ast.Store):
        reserved = name == "api" or name == "plan" or name in ALLOWED_BUILTINS
        return [f"cannot reassign reserved name: {name}"] if reserved else []
    if name == "api" or name in ALLOWED_BUILTINS or name in assigned:
        return []
    return [f"forbidden name: {name}"]


def _call_errors(node: ast.Call) -> list[str]:
    function = node.func
    if isinstance(function, ast.Name):
        return [] if function.id in ALLOWED_BUILTINS else [f"forbidden call: {function.id}"]
    if isinstance(function, ast.Attribute):
        return []
    return ["only named builtins and allowed methods may be called"]


def _constant_errors(node: ast.Constant) -> list[str]:
    value = node.value
    if value is None or isinstance(value, bool | float):
        return []
    if isinstance(value, int):
        return [] if abs(value) <= MAX_INT_LITERAL else ["integer literal is too large"]
    if isinstance(value, str):
        return [] if len(value) <= MAX_STRING_LITERAL else ["string literal is too long"]
    return [f"forbidden constant type: {type(value).__name__}"]
