"""Find readable values in columns declared as encrypted.

The audit reads raw column values only. It never loads a key and never calls a
decryption routine, so it can be run against a production database without
making sensitive values available to the process.
"""

from __future__ import annotations

from typing import Any

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError

from apps.core.crypto import is_encrypted_value
from apps.core.encrypted_fields import EncryptedFieldsMixin


class Command(BaseCommand):
    help = "Audit encrypted columns for readable values without decrypting them."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--marker",
            action="append",
            default=[],
            help="Known plaintext marker to check (may be supplied more than once).",
        )
        parser.add_argument(
            "--fail-on-findings",
            action="store_true",
            help="Exit with an error when readable values are found.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        markers = tuple(str(marker) for marker in options["marker"] if marker)
        findings: list[tuple[str, str, int]] = []

        for discovered_model in apps.get_models():
            model: Any = discovered_model
            if not issubclass(model, EncryptedFieldsMixin) or not model.encrypted_fields:
                continue
            for field in model.encrypted_fields:
                count = 0
                for value in model.objects.values_list(field, flat=True).iterator():
                    if not value or is_encrypted_value(value):
                        continue
                    # Any non-envelope is readable. Markers are an additional
                    # signal for callers documenting a known fixture value.
                    if not markers or any(marker in str(value) for marker in markers):
                        count += 1
                if count:
                    findings.append((model._meta.label, field, count))

        if not findings:
            self.stdout.write("Plaintext audit: clean.")
            return

        self.stdout.write("Plaintext audit findings:")
        for model, field, count in findings:
            self.stdout.write(f"- {model}.{field}: {count} readable value(s)")
        if options["fail_on_findings"]:
            raise CommandError(f"Found readable values in {len(findings)} encrypted field(s).")
