from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import ast
import json
import operator
import time
from typing import Any, Callable

from .models import Risk


class ToolValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Tool:
    name: str
    version: str
    risk: Risk
    validator: Callable[[dict[str, Any]], dict[str, Any]]
    executor: Callable[[dict[str, Any]], Any]

    def run(self, raw: dict[str, Any]) -> tuple[Any, float]:
        value = self.validator(raw)
        started = time.perf_counter()
        return self.executor(value), (time.perf_counter() - started) * 1000


def _only(raw: dict[str, Any], key: str, typ: type) -> dict[str, Any]:
    if set(raw) != {key} or not isinstance(raw[key], typ):
        raise ToolValidationError(f"expected exactly {key}: {typ.__name__}")
    return raw


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow, ast.USub: operator.neg}


def _calculate(node: ast.AST) -> Decimal:
    if isinstance(node, ast.Expression): return _calculate(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return Decimal(str(node.value))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS: return _OPS[type(node.op)](_calculate(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _calculate(node.left), _calculate(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 20: raise ToolValidationError("exponent too large")
        return _OPS[type(node.op)](left, right)
    raise ToolValidationError("unsupported expression")


def calculator(v: dict[str, Any]) -> dict[str, str]:
    try: result = _calculate(ast.parse(v["expression"], mode="eval"))
    except (SyntaxError, InvalidOperation, ZeroDivisionError, OverflowError) as exc: raise ToolValidationError(str(exc)) from exc
    return {"result": format(result, "f").rstrip("0").rstrip(".") or "0"}


def registry() -> dict[str, Tool]:
    def list_numbers(v: dict[str, Any]) -> dict[str, Any]:
        if set(v) != {"items"} or not isinstance(v["items"], list) or any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in v["items"]):
            raise ToolValidationError("items must be a numeric list")
        return v
    def filter_validator(v: dict[str, Any]) -> dict[str, Any]:
        if set(v) != {"items", "equals"} or not isinstance(v["items"], list): raise ToolValidationError("items and equals required")
        return v
    return {
        "calculator": Tool("calculator", "1.0", Risk.SAFE, lambda v: _only(v, "expression", str), calculator),
        "json_validate": Tool("json_validate", "1.0", Risk.SAFE, lambda v: _only(v, "text", str), lambda v: {"valid": True, "value": json.loads(v["text"])}),
        "sort_ascending": Tool("sort_ascending", "1.0", Risk.SAFE, list_numbers, lambda v: {"items": sorted(v["items"])}),
        "list_filter": Tool("list_filter", "1.0", Risk.SAFE, filter_validator, lambda v: {"items": [x for x in v["items"] if x == v["equals"]]}),
        "lowercase": Tool("lowercase", "1.0", Risk.SAFE, lambda v: _only(v, "text", str), lambda v: {"text": v["text"].lower()}),
    }
