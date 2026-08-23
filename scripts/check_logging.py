"""Reject logging calls that can carry value-bearing or secret fields."""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

LOG_METHODS = frozenset({"debug", "info", "warning", "error", "exception", "critical", "log"})
SENSITIVE_NAMES = frozenset(
    {
        "merchant",
        "amount",
        "counterparty",
        "ocr_text",
        "account_number",
        "card_number",
        "last_four",
        "screenshot",
        "filename",
        "password",
        "secret",
        "token",
        "authorization",
        "key",
        "otp_secret",
        "recovery_code",
        "cookie",
        "cookies",
        "session_key",
        "csrf_token",
        "access_token",
        "refresh_token",
        "api_key",
        "raw_output",
        "raw_text",
        "approval_code",
        "exc",
        "exception",
        "error",
        "err",
    }
)
SENSITIVE_PREFIXES = (
    "merchant_",
    "amount_",
    "counterparty_",
    "ocr_",
    "account_",
    "card_",
    "screenshot_",
    "filename_",
    "approval_",
    "password_",
    "secret_",
    "token_",
)
SENSITIVE_SUFFIXES = ("_password", "_secret", "_token", "_key")
SAFE_EXCEPTION_ATTRIBUTES = frozenset(
    {"code", "status_code", "public_message", "public_recovery_hint"}
)
SENSITIVE_ASSIGNMENT = re.compile(
    r"\b(?:merchant|amount|counterparty|ocr_text|account_number|card_number|"
    r"last_four|screenshot|filename|password|secret|token|authorization|key|"
    r"otp_secret|recovery_code|cookie|cookies|session_key|csrf_token|access_token|"
    r"refresh_token|api_key|raw_output|approval_code)\b\s*[:=]",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class LoggingViolation:
    path: Path
    line: int
    column: int
    reason: str


def _sensitive_name(name: str) -> bool:
    normalized = name.casefold()
    return (
        normalized in SENSITIVE_NAMES
        or normalized.startswith(SENSITIVE_PREFIXES)
        or normalized.endswith(SENSITIVE_SUFFIXES)
    )


def _sensitive_nodes(node: ast.AST) -> Iterable[str]:
    attribute_bases = {
        id(child.value) for child in ast.walk(node) if isinstance(child, ast.Attribute)
    }
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and _sensitive_name(child.id):
            if id(child) not in attribute_bases:
                yield child.id
        elif isinstance(child, ast.Attribute):
            if _sensitive_name(child.attr):
                yield child.attr
            elif (
                isinstance(child.value, ast.Name)
                and _sensitive_name(child.value.id)
                and child.attr not in SAFE_EXCEPTION_ATTRIBUTES
            ):
                yield child.value.id
        elif (
            isinstance(child, ast.Constant)
            and isinstance(child.value, str)
            and _sensitive_name(child.value)
        ):
            yield child.value


def _message_is_sensitive(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and bool(SENSITIVE_ASSIGNMENT.search(node.value))
    )


class _LoggingVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.violations: list[LoggingViolation] = []

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and node.func.attr in LOG_METHODS:
            self._inspect_logging_call(node)
        self.generic_visit(node)

    def _inspect_logging_call(self, node: ast.Call) -> None:
        if node.args and _message_is_sensitive(node.args[0]):
            self._add(node, "log message names a sensitive field")
            return

        values = list(node.args[1:])
        values.extend(keyword.value for keyword in node.keywords if keyword.arg != "exc_info")
        for value in values:
            names = tuple(dict.fromkeys(_sensitive_nodes(value)))
            if names:
                self._add(node, f"log argument may contain sensitive field: {names[0]}")
                return

    def _add(self, node: ast.Call, reason: str) -> None:
        self.violations.append(
            LoggingViolation(self.path, node.lineno, node.col_offset + 1, reason)
        )


def check_paths(paths: Iterable[Path]) -> tuple[LoggingViolation, ...]:
    violations: list[LoggingViolation] = []
    for root in paths:
        files = (root,) if root.is_file() else root.rglob("*.py")
        for path in files:
            if any(part in {"__pycache__", ".venv"} for part in path.parts):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError as exc:
                violations.append(
                    LoggingViolation(path, exc.lineno or 1, exc.offset or 1, str(exc))
                )
                continue
            visitor = _LoggingVisitor(path)
            visitor.visit(tree)
            violations.extend(visitor.violations)
    return tuple(violations)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    violations = check_paths(root / name for name in ("apps", "config", "scripts"))
    if not violations:
        print("Logging source check passed.")
        return 0
    for violation in violations:
        print(
            f"{violation.path}:{violation.line}:{violation.column}: {violation.reason}",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
