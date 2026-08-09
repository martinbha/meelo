from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.categorization.models import Category
from apps.financial_accounts.models import FinancialAccount
from apps.users.models import User


class Command(BaseCommand):
    help = "Load safe starter categories and a representative account for a user."

    def add_arguments(self, parser) -> None:  # type: ignore[no-untyped-def]
        parser.add_argument("--email", required=True, help="Email address of the target user.")

    @transaction.atomic
    def handle(self, *args: object, **options: object) -> None:
        email = str(options["email"]).strip().lower()
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist as exc:
            raise CommandError(f"No user exists for {email}.") from exc

        categories = (
            ("Groceries", "fixture-groceries", Category.CategoryType.EXPENSE),
            ("Salary", "fixture-salary", Category.CategoryType.INCOME),
            ("Transfers", "fixture-transfers", Category.CategoryType.TRANSFER),
        )
        for name, blind_index, category_type in categories:
            Category.objects.get_or_create(
                user=user,
                name_blind_index=blind_index,
                parent=None,
                defaults={
                    "name_encrypted": name,
                    "category_type": category_type,
                    "is_system": True,
                },
            )
        FinancialAccount.objects.get_or_create(
            user=user,
            name_blind_index="fixture-checking",
            defaults={
                "name_encrypted": "Checking account",
                "institution_encrypted": "Example bank",
                "institution_blind_index": "fixture-bank",
                "account_type": FinancialAccount.AccountType.CHECKING,
                "currency": "KRW",
            },
        )
        self.stdout.write(self.style.SUCCESS(f"Loaded domain fixtures for {email}."))
