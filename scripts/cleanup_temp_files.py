"""Clean stale document directories and expired export files.

Run this with ``uv run python scripts/cleanup_temp_files.py`` from the project
root. ``--dry-run`` selects the candidates and prints counts without deleting;
the real run consumes that same selection logic.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from datetime import timedelta

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")

import django

django.setup()

from django.utils import timezone  # noqa: E402

from apps.processing.cleanup import run_cleanup  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--age-hours", type=int, default=24)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    options = _parser().parse_args(argv)
    age_hours = max(options.age_hours, 1)
    now = timezone.now()
    report = run_cleanup(
        cutoff=now - timedelta(hours=age_hours), now=now, dry_run=options.dry_run
    )
    document_count = sum(candidate.kind == "document_directory" for candidate in report.candidates)
    export_count = sum(candidate.kind == "export_file" for candidate in report.candidates)
    prefix = "DRY_RUN" if options.dry_run else "CLEANUP"
    print(
        f"{prefix} candidates={len(report.candidates)} "
        f"document_directories={document_count} export_files={export_count} "
        f"removed={report.removed} failed={report.failed}"
    )
    if report.failed:
        print("CLEANUP_FAILED")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
