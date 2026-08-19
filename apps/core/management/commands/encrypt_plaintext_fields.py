"""Seal the rows that were written before their column was encrypted.

Encryption reached several models after their first rows existed, so both forms
sit in the same column: an envelope, and a readable merchant name that looks
exactly like one. Nothing distinguishes them at a glance, and nothing will
report the difference — a plaintext amount adds up perfectly.

This walks every column the models declare as encrypted, finds the values that
are not envelopes, and seals them under the owner's current key.

**One-way.** There is no reverse. Decrypting the whole database back to plaintext
is not an operation this application should be able to perform on request, so
the way back is a restore from the backup taken before the run — which is why
the command says so before it starts and why ``--dry-run`` exists.

**Bounded and resumable.** Rows are walked in primary-key order with a keyset
cursor, a page at a time, each page in its own transaction. An interruption
leaves the sealed rows sealed; the next run finds only what is left, because the
remaining work is a query — an unsealed value is one that is not an envelope —
rather than a saved position.

Operational columns stay readable on purpose. A queue cannot select on a
ciphertext, an index cannot order one, and a status nobody can read is a status
the worker cannot act on. What is encrypted is what the specification's
encrypted-field list names, and that list is what the models declare.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction as db_transaction

from apps.core.crypto import is_encrypted_value
from apps.core.encrypted_fields import EncryptedFieldsMixin
from apps.core.key_management import get_user_data_key, load_master_key
from apps.users.models import User


@dataclass(slots=True)
class SealingReport:
    rows_examined: int = 0
    values_sealed: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.failures


@dataclass(frozen=True, slots=True)
class SealableModel:
    """One model, and how to reach the rows belonging to a given user."""

    label: str
    owner_filter: str = "user_id"

    def model(self) -> Any:
        from django.apps import apps

        app_label, model_name = self.label.split(".")
        return apps.get_model(app_label, model_name)


def sealable_models() -> tuple[SealableModel, ...]:
    """Every model with encrypted columns, discovered rather than listed.

    Read from the declarations so a model that gains an encrypted column is
    covered without anybody remembering to add it here. A hand-kept list would
    silently omit exactly the newest column, which is the one most likely to
    still hold plaintext.
    """

    from django.apps import apps

    #: Records that carry no owner column reach one through a relation.
    owner_filters = {"ledger.LedgerEntry": "transaction__user_id"}
    discovered = []
    for model in apps.get_models():
        if not model.__module__.startswith("apps."):
            continue
        if not issubclass(model, EncryptedFieldsMixin) or not model.encrypted_fields:
            continue
        label = model._meta.label
        discovered.append(SealableModel(label, owner_filters.get(label, "user_id")))
    return tuple(sorted(discovered, key=lambda spec: spec.label))


def _pages(queryset: Any, size: int) -> Iterator[Sequence[Any]]:
    last_pk: Any = None
    while True:
        page = queryset.order_by("pk")
        if last_pk is not None:
            page = page.filter(pk__gt=last_pk)
        batch = list(page[:size])
        if not batch:
            return
        yield batch
        last_pk = batch[-1].pk


def seal_user(
    *,
    user: User,
    data_key: bytes,
    key_version: int,
    models: Sequence[SealableModel] | None = None,
    batch_size: int = 500,
    dry_run: bool = False,
) -> SealingReport:
    """Encrypt every readable value in one user's encrypted columns."""

    report = SealingReport()
    for spec in models if models is not None else sealable_models():
        model = spec.model()
        for page in _pages(model.objects.filter(**{spec.owner_filter: user.pk}), batch_size):
            with db_transaction.atomic():
                for record in page:
                    plaintexts: dict[str, str] = {}
                    for column in record.encrypted_fields:
                        report.rows_examined += 1
                        stored = getattr(record, column) or ""
                        if not stored or is_encrypted_value(stored):
                            continue
                        plaintexts[column] = stored
                    if not plaintexts:
                        continue
                    try:
                        record.encrypt_fields(plaintexts, key=data_key, key_version=key_version)
                    except Exception as error:  # noqa: BLE001 - reported, not raised
                        report.failures.append(f"{spec.label}:{record.pk}: {error}")
                        continue
                    if not dry_run:
                        model.objects.filter(pk=record.pk).update(
                            **{column: getattr(record, column) for column in plaintexts}
                        )
                    report.values_sealed += len(plaintexts)
    return report


class Command(BaseCommand):
    help = "Encrypt values still stored in clear in encrypted columns. One-way."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--email", help="Limit the run to one user.")
        parser.add_argument("--batch-size", type=int, default=500)
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be sealed without writing anything.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        batch_size = max(int(options["batch_size"]), 1)
        dry_run = bool(options["dry_run"])
        master_key = load_master_key()
        users = User.objects.all().order_by("email")
        if options["email"]:
            users = users.filter(email=str(options["email"]).strip().lower())
            if not users.exists():
                raise CommandError(f"No user exists for {options['email']}.")

        if not dry_run:
            self.stdout.write(
                self.style.WARNING(
                    "This is one-way. The way back is a restore from the backup taken "
                    "before it. Run with --dry-run first if you have not."
                )
            )

        for user in users:
            data_key = get_user_data_key(user=user, actor=user, master_key=master_key)
            report = seal_user(
                user=user,
                data_key=data_key,
                key_version=user.encryption_key_version,
                batch_size=batch_size,
                dry_run=dry_run,
            )
            verb = "would seal" if dry_run else "sealed"
            self.stdout.write(
                f"{user.email}: examined {report.rows_examined} value(s), "
                f"{verb} {report.values_sealed}."
            )
            for failure in report.failures:
                self.stderr.write(f"  failed {failure}")
