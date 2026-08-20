"""Enforce project and critical-module coverage floors from coverage.py JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

PROJECT_MINIMUM = 85.0
CRITICAL_MINIMUMS = {
    "apps/core": 90.0,
    "apps/ledger": 90.0,
    "apps/reconciliation": 90.0,
    "apps/reports": 95.0,
}


def _module_coverage(files: dict[str, Any], prefix: str) -> float:
    covered = statements = 0
    for filename, payload in files.items():
        if not filename.startswith(f"{prefix}/"):
            continue
        if "/migrations/" in filename or filename.endswith("/__init__.py"):
            continue
        summary = payload.get("summary", {})
        covered += int(summary.get("covered_lines", 0))
        statements += int(summary.get("num_statements", 0))
    return 100.0 if statements == 0 else 100.0 * covered / statements


def check_coverage(payload: dict[str, Any]) -> list[str]:
    """Return human-readable failures without printing source values or paths."""

    failures: list[str] = []
    total = payload.get("totals", {})
    project = float(total.get("percent_covered", 0.0))
    if project < PROJECT_MINIMUM:
        failures.append(f"project coverage {project:.1f}% is below {PROJECT_MINIMUM:.1f}%")

    files = payload.get("files", {})
    for prefix, minimum in CRITICAL_MINIMUMS.items():
        actual = _module_coverage(files, prefix)
        if actual < minimum:
            failures.append(f"{prefix} coverage {actual:.1f}% is below {minimum:.1f}%")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="coverage.py JSON report")
    args = parser.parse_args()
    payload = json.loads(args.report.read_text(encoding="utf-8"))
    failures = check_coverage(payload)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print(
        "Coverage gates passed: "
        f"project >= {PROJECT_MINIMUM:.1f}%, "
        + ", ".join(f"{name} >= {minimum:.1f}%" for name, minimum in CRITICAL_MINIMUMS.items())
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
