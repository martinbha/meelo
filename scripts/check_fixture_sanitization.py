"""Reject fixture files containing obvious unmasked account identifiers."""

from __future__ import annotations

import re
import sys
from pathlib import Path

FIXTURE_ROOT = Path("tests/fixtures")
IDENTIFIER_CANDIDATE = re.compile(r"(?<![\d*])\d{2,6}(?:-\d{2,8}){2,}(?![\d*])")
TEXT_SUFFIXES = {".csv", ".json", ".pbm", ".txt"}


def find_unsanitized_identifiers(path: Path) -> list[str]:
    """Return unmasked, account-shaped identifiers found in a text fixture."""
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return []
    text = path.read_text(encoding="utf-8")
    candidates = (match.group(0) for match in IDENTIFIER_CANDIDATE.finditer(text))
    return [
        candidate
        for candidate in candidates
        if sum(character.isdigit() for character in candidate) >= 10
    ]


def fixture_paths(root: Path = FIXTURE_ROOT) -> list[Path]:
    """Return fixture files in stable order."""
    return sorted(path for path in root.rglob("*") if path.is_file())


def main() -> int:
    findings = [
        (path, identifier)
        for path in fixture_paths()
        for identifier in find_unsanitized_identifiers(path)
    ]
    for path, identifier in findings:
        print(f"{path}: plausible unmasked account identifier: {identifier}")
    if findings:
        print("Replace identifiers with synthetic, masked values before committing.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
