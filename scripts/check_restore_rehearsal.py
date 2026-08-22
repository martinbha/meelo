"""Compare restored database row counts with the archive manifest."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")

import django

django.setup()

from django.apps import apps  # noqa: E402

from apps.core.backup import BACKED_UP_APPS  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("unpacked", type=Path, help="Directory produced by restore_database.sh.")
    options = parser.parse_args(argv)
    manifest = json.loads((options.unpacked / "manifest.json").read_text(encoding="utf-8"))
    expected = manifest["row_counts"]
    actual = {
        f"{model._meta.app_label}.{model._meta.model_name}": model.objects.count()
        for app_label in BACKED_UP_APPS
        for model in apps.get_app_config(app_label).get_models()
    }
    mismatches = {
        key: (expected.get(key, 0), actual.get(key, 0))
        for key in sorted(set(expected) | set(actual))
        if expected.get(key, 0) != actual.get(key, 0)
    }
    if mismatches:
        for key, (wanted, found) in mismatches.items():
            print(f"RESTORE_REHEARSAL_FAILED {key} expected={wanted} actual={found}")
        return 1
    print(f"RESTORE_REHEARSAL_OK rows={sum(actual.values())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
