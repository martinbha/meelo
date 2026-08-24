from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.core.key_management import (
    get_user_data_key,
    get_user_search_key,
    load_master_key,
)
from apps.observations.models import ImportedObservation
from apps.observations.review import decrypt_observation
from apps.reconciliation.duplicates import find_duplicate_candidates
from apps.reconciliation.models import ReconciliationMatch
from apps.reconciliation.refunds import propose_refund_matches
from apps.reconciliation.services import facts_from, record_duplicate_candidates
from apps.reconciliation.transfers import propose_internal_transfers
from apps.users.models import User


class Command(BaseCommand):
    help = "Generate bounded reconciliation candidates for users and a date range."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--email")
        parser.add_argument("--start", type=date.fromisoformat)
        parser.add_argument("--end", type=date.fromisoformat)

    def handle(self, *args: Any, **options: Any) -> None:
        end: date = options["end"] or timezone.localdate()
        start: date = options["start"] or end - timedelta(days=30)
        if end < start:
            raise CommandError("The end date cannot precede the start date.")
        master_key = load_master_key()
        users = User.objects.filter(is_active=True).order_by("pk")
        if options["email"]:
            users = users.filter(email__iexact=str(options["email"]).strip())
            if not users.exists():
                raise CommandError("No active user exists for that email address.")

        totals = [0, 0, 0, 0]
        for user in users:
            result = self._generate(user, start=start, end=end, master_key=master_key)
            totals = [current + value for current, value in zip(totals, result, strict=True)]
        self.stdout.write(
            self.style.SUCCESS(
                f"examined={totals[0]} created={totals[1]} existing={totals[2]} skipped={totals[3]}"
            )
        )

    def _generate(
        self, user: User, *, start: date, end: date, master_key: bytes
    ) -> tuple[int, int, int, int]:
        data_key = get_user_data_key(user=user, actor=user, master_key=master_key)
        search_key = get_user_search_key(user=user, actor=user, master_key=master_key)
        rows = list(
            ImportedObservation.objects.filter(
                user=user,
                occurred_at__range=(start, end),
            ).select_related("source_document")
        )
        before = set(ReconciliationMatch.objects.filter(user=user).values_list("pk", flat=True))
        facts = []
        for row in rows:
            values = decrypt_observation(row, user=user, data_key=data_key)
            facts.append(
                facts_from(
                    row,
                    merchant=values.merchant,
                    amount_minor=values.amount_minor,
                    approval_code=values.approval_code,
                    balance_after_minor=(
                        values.balance_after.amount_minor
                        if values.balance_after is not None
                        else None
                    ),
                    source_type=row.source_document.effective_source_type,
                )
            )

        generated = list(
            record_duplicate_candidates(
                user=user,
                candidates=find_duplicate_candidates(facts, search_key=search_key),
                data_key=data_key,
                key_version=user.encryption_key_version,
            )
        )
        generated.extend(
            propose_internal_transfers(
                user=user,
                data_key=data_key,
                key_version=user.encryption_key_version,
                start_date=start,
                end_date=end,
            )
        )
        generated.extend(
            propose_refund_matches(
                user=user,
                data_key=data_key,
                key_version=user.encryption_key_version,
                start_date=start,
                end_date=end,
            )
        )
        generated_ids = {item.pk for item in generated}
        created = len(generated_ids - before)
        existing = len(generated_ids & before)
        skipped = max(len(rows) - len(generated_ids), 0)
        return len(rows), created, existing, skipped
