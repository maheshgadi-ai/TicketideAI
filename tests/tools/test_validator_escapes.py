"""Escape attempts the AST allowlist must reject before any code runs."""

from __future__ import annotations

import pytest

from ticketideai.tools.validator import MAX_SOURCE_CHARS, PlanValidationError, PlanValidator


def body(*lines: str) -> str:
    return "def plan(api):\n" + "\n".join(f"    {line}" for line in lines) + "\n"


DUNDER_WALKS = {
    "tuple_subclasses": body("return ().__class__.__bases__[0].__subclasses__()"),
    "str_mro_subclasses": body('return "".__class__.__mro__[1].__subclasses__()'),
    "api_class": body("return api.__class__"),
    "api_dict": body("return api.__dict__"),
    "api_init_globals": body("return api.__init__.__globals__"),
    "plan_globals": body("return plan.__globals__"),
    "plan_code": body("return plan.__code__"),
    "bound_method_self": body("return api.get_order.__self__"),
    "bound_method_func": body("return api.get_order.__func__"),
    "reduce_ex": body("return [].__reduce_ex__(2)"),
    "result_fields": body('return api.check_stock("RIDGE-001").__dict__'),
    "mro_method": body("return str.mro()"),
    "dunder_name": body("return __builtins__"),
    "dunder_import": body('return __import__("os")'),
    "loader_name": body("return __loader__"),
    "spec_name": body("return __spec__"),
    "private_name": body("_hidden = 1", "return _hidden"),
}
DYNAMIC_ACCESS = {
    "getattr_string": body('return getattr(api, "__class__")'),
    "getattr_engine": body('return getattr(api, "engine")'),
    "setattr": body('setattr(api, "x", 1)'),
    "delattr": body('delattr(api, "get_order")'),
    "hasattr": body('return hasattr(api, "engine")'),
    "vars": body("return vars(api)"),
    "dir": body("return dir(api)"),
    "globals": body("return globals()"),
    "locals": body("return locals()"),
    "type": body("return type(api)"),
    "object": body("return object"),
    "super": body("return super()"),
    "exec": body('exec("1")'),
    "eval": body('eval("1")'),
    "compile": body('compile("1", "x", "eval")'),
    "open": body('open("/etc/passwd")'),
    "input": body("input()"),
    "breakpoint": body("breakpoint()"),
    "memoryview": body('memoryview(b"x")'),
    "print": body('print("x")'),
}
GENERATOR_AND_FRAME = {
    "generator_expression": body("return (x for x in [1]).gi_frame"),
    "gi_frame_attr": body("x = [1]", "return x.gi_frame"),
    "gi_code_attr": body("x = [1]", "return x.gi_code"),
    "cr_frame_attr": body("x = [1]", "return x.cr_frame"),
    "ag_frame_attr": body("x = [1]", "return x.ag_frame"),
    "f_globals_attr": body("x = [1]", "return x.f_globals"),
    "f_builtins_attr": body("x = [1]", "return x.f_builtins"),
    "f_back_attr": body("x = [1]", "return x.f_back"),
    "tb_frame_attr": body("x = [1]", "return x.tb_frame"),
    "yield": body("yield 1"),
    "yield_from": body("yield from [1]"),
    "await": "async def plan(api):\n    await api\n",
    "traceback_via_except": body(
        "try:", "    x = 1", "except Exception as e:", "    return e.__traceback__"
    ),
}
FORMAT_STRINGS = {
    "format_class": body('return "{0.__class__}".format(api)'),
    "format_engine": body('return "{0.engine}".format(api)'),
    "str_format": body('return str.format("{0.__class__}", api)'),
    "format_map": body('return "{a.__class__}".format_map({"a": api})'),
    "fstring_dunder": body('return f"{api.__class__}"'),
    "fstring_engine": body('return f"{api.engine}"'),
    "fstring_conversion": body('return f"{().__class__!r}"'),
    "fstring_nested_spec": body("x = 1", 'return f"{x:{api.__class__}}"'),
}
LAMBDA_AND_STRUCTURE = {
    "lambda": body("return (lambda: 1)()"),
    "lambda_default": body("return (lambda a=api: a)()"),
    "default_argument": "def plan(api, x=().__class__):\n    return x\n",
    "api_default": "def plan(api=1):\n    return api\n",
    "extra_argument": "def plan(api, other):\n    return other\n",
    "varargs": "def plan(api, *args):\n    return args\n",
    "kwargs": "def plan(api, **kwargs):\n    return kwargs\n",
    "keyword_only": "def plan(api, *, x=1):\n    return x\n",
    "decorator": "@len\ndef plan(api):\n    return 1\n",
    "annotation": "def plan(api: ().__class__):\n    return 1\n",
    "return_annotation": "def plan(api) -> ().__class__:\n    return 1\n",
    "nested_function": body("def inner():", "    return 1", "return inner()"),
    "helper_function": "def helper():\n    return 1\ndef plan(api):\n    return helper()\n",
    "class_definition": body("class X:", "    pass"),
    "self_recursion": body("return plan(api)"),
    "list_comprehension": body("return [x for x in [1]]"),
    "dict_comprehension": body("return {x: 1 for x in [1]}"),
    "set_comprehension": body("return {x for x in [1]}"),
    "walrus": body("return (x := 1)"),
    "star_call": body("return len(*[[1]])"),
    "double_star_call": body("return dict(**{})"),
    "module_statement": "x = 1\ndef plan(api):\n    return x\n",
    "two_plans": "def plan(api):\n    return 1\ndef plan(api):\n    return 2\n",
    "no_plan": "def other(api):\n    return 1\n",
    "import": "import os\ndef plan(api):\n    return os\n",
    "import_inside": body("import os"),
    "from_import": body("from os import system"),
    "global": body("global x"),
    "delete": body("x = 1", "del x"),
    "with": body('with open("x") as f:', "    pass"),
    "try": body("try:", "    pass", "except Exception:", "    pass"),
    "raise": body('raise ValueError("x")'),
    "assert": body("assert True"),
    "while": body("while True:", "    pass"),
    "match": body("match 1:", "    case 1:", "        pass"),
    "power_bomb": body("return 9 ** 9 ** 9"),
    "shift_bomb": body("return 1 << 100000000"),
}
UNICODE_IDENTIFIERS = {
    "fullwidth_api": body("return ａｐｉ.get_order(1)"),
    "fullwidth_dunder": body("return ().__ｃｌａｓｓ__"),
    "fullwidth_eval": body('return ｅｖａｌ("1")'),
    "fullwidth_getattr": body('return ｇｅｔａｔｔｒ(api, "x")'),
    "math_bold_exec": body('return 𝐞𝐱𝐞𝐜("1")'),
    "fullwidth_engine": body("return api.ｅｎｇｉｎｅ"),
    "fullwidth_variable": body("ｘ = 1", "return ｘ"),
    "ligature_name": body("ﬁ = 1", "return ﬁ"),
}
API_SURFACE = {
    "engine_attribute": body("return api.engine"),
    "recorder_attribute": body("return api._recorder"),
    "private_session": body("return api._session()"),
    "unknown_method": body("api.delete_database()"),
    "approve_method": body("api.approve(1)"),
    "commit_method": body("api.commit()"),
    "method_alias": body("refund = api.issue_refund", "refund(1, 500)"),
    "method_in_list": body("return [api.issue_refund][0](1, 500)"),
    "call_result": body("return api.get_order(1)()"),
    "reassign_api": body("api = 1"),
    "reassign_builtin": body("len = 1"),
    "reassign_plan": body("plan = 1"),
    "mutate_attribute": body("order = api.get_order(1)", 'order.status = "returned"'),
    "api_method_on_other": body("x = api", "x.issue_refund(1, 500)"),
    "bytes_literal": body('return b"x"'),
    "huge_int_literal": body("return 99999999999999"),
    "huge_string_literal": body(f'return "{"x" * 5000}"'),
}
MALFORMED = {
    "empty": "",
    "blank": "   \n  ",
    "syntax_error": "def plan(api:\n",
    "null_byte": "def plan(api):\n    return 1\x00\n",
    "oversized": "def plan(api):\n    return 1\n" + "#" * MAX_SOURCE_CHARS,
    "deep_parentheses": "def plan(api):\n    return " + "(" * 5000 + "1" + ")" * 5000 + "\n",
    "too_many_nodes": body(*["x = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]"] * 400),
}
ALL_ESCAPES = {
    **DUNDER_WALKS,
    **DYNAMIC_ACCESS,
    **GENERATOR_AND_FRAME,
    **FORMAT_STRINGS,
    **LAMBDA_AND_STRUCTURE,
    **UNICODE_IDENTIFIERS,
    **API_SURFACE,
    **MALFORMED,
}


def test_suite_covers_more_than_fifty_attempts() -> None:
    assert len(ALL_ESCAPES) > 50


@pytest.mark.parametrize("source", ALL_ESCAPES.values(), ids=ALL_ESCAPES.keys())
def test_escape_attempt_is_rejected(source: str) -> None:
    with pytest.raises(PlanValidationError):
        PlanValidator().validate_or_raise(source)


@pytest.mark.parametrize(
    "source",
    [
        body("order = api.get_order(1)", 'return {"outcome": "done", "status": order.status}'),
        body("total = 0", "for line in api.get_order(1).lines:", "    total += line.quantity"),
        body("names = []", 'names.append("a")', 'return {"summary": ", ".join(names)}'),
        body('text = f"Order {api.get_order(1).id}"', 'return {"summary": text.upper()}'),
        body("api.issue_refund(1, round(25.0 * 2, 2))", 'return {"outcome": "done"}'),
    ],
)
def test_ordinary_plans_are_allowed(source: str) -> None:
    PlanValidator().validate_or_raise(source)
