"""Encrypted-amount reports: correct, and fast enough (#90, spec 22.5, 25.4).

The database cannot add encrypted amounts up, so the application decrypts row by
row. The tempting fix is caching the totals, and that is the one thing this design
cannot afford: a cached total is a plaintext copy of somebody's finances outside
the encrypted store. So the cost is measured rather than assumed, and the
correctness is checked against a number a person with a calculator would reach.
"""

from __future__ import annotations

import base64
import os
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from apps.categorization.models import Category
from apps.core.crypto import decrypt_model_field, encrypt_model_field
from apps.core.key_management import get_user_data_key, provision_user_data_key
from apps.instruments.models import PaymentInstrument
from apps.reports.benchmark import (
    BUDGET,
    SNAPSHOT_REVIEW_THRESHOLD,
    ReportTimings,
    decrypted_total,
    measure_report,
)
from apps.reports.breakdown import category_breakdown, merchant_breakdown, reconciles
from apps.reports.overview import period_overview
from apps.reports.spending import month_bounds, monthly_spending
from apps.transactions.models import CanonicalTransaction
from tests.factories import make_account, make_user

pytestmark = pytest.mark.django_db

_Type = CanonicalTransaction.TransactionType
AUGUST = month_bounds(2026, 8)

#: Enough rows for a timing to mean something without making the suite slow.
#: A heavy personal year is a few thousand transactions; this is a month's worth
#: of a very busy one.
FIXTURE_ROWS = 600

#: The mix a real month has: mostly purchases, some of everything else. A fixture
#: of pure purchases would not exercise the bucket dispatch that the totals
#: depend on.
TYPE_MIX: tuple[tuple[str, int], ...] = (
    (_Type.PURCHASE, 60),
    (_Type.REFUND, 8),
    (_Type.INCOME, 4),
    (_Type.INTERNAL_TRANSFER, 8),
    (_Type.CREDIT_CARD_PAYMENT, 8),
    (_Type.CASH_WITHDRAWAL, 6),
    (_Type.FEE, 3),
    (_Type.INTEREST, 1),
    (_Type.ADJUSTMENT, 1),
    (_Type.UNKNOWN, 1),
)


@pytest.fixture
def master_key(tmp_path: Path, settings: Any) -> bytes:
    key = os.urandom(32)
    path = tmp_path / "master.key"
    path.write_text(base64.urlsafe_b64encode(key).decode(), encoding="ascii")
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)
    settings.DOCUMENT_TMP_ROOT = str(tmp_path / "documents")
    return key


@pytest.fixture
def owner(master_key: bytes) -> Any:
    user = make_user(email="performance-owner@example.com")
    provision_user_data_key(user=user, actor=user, master_key=master_key)
    return user


@pytest.fixture
def data_key(owner: Any, master_key: bytes) -> bytes:
    return get_user_data_key(user=owner, actor=owner, master_key=master_key)


def type_sequence(count: int) -> list[str]:
    """The transaction types for one fixture month, in a repeating mix."""

    pattern = [kind for kind, weight in TYPE_MIX for _ in range(weight)]
    return [pattern[index % len(pattern)] for index in range(count)]


@pytest.fixture
def encrypted_month(owner: Any, data_key: bytes) -> dict[str, Any]:
    """A month of transactions with every amount genuinely encrypted.

    Built once per test that needs it. The amounts are distinct so a bug that
    counted one row twice, or dropped one, changes the total rather than hiding
    inside equal values.
    """

    account = make_account(owner, name_encrypted="checking", name_blind_index="perf-checking")
    card = PaymentInstrument.objects.create(
        user=owner,
        name_encrypted="visa",
        name_blind_index="perf-visa",
        instrument_type=PaymentInstrument.InstrumentType.CREDIT_CARD,
        financial_account=account,
    )
    categories = [
        Category.objects.create(
            user=owner,
            name_encrypted=f"category-{index}",
            name_blind_index=f"perf-category-{index}",
            category_type=Category.CategoryType.EXPENSE,
        )
        for index in range(6)
    ]

    kinds = type_sequence(FIXTURE_ROWS)
    created: list[CanonicalTransaction] = []
    for index, kind in enumerate(kinds):
        transaction = CanonicalTransaction.objects.create(
            user=owner,
            created_by=owner,
            financial_account=account,
            payment_instrument=card if index % 3 else None,
            category=categories[index % len(categories)] if index % 5 else None,
            occurred_at=date(2026, 8, (index % 28) + 1),
            amount_encrypted="0:KRW",
            currency="KRW",
            transaction_type=kind,
            merchant_encrypted="",
            merchant_blind_index=f"perf-merchant-{index % 40}",
        )
        # Distinct amounts, so a double count or a dropped row moves the total.
        minor = 1_000 + index
        transaction.amount_encrypted = encrypt_model_field(
            transaction, "amount_encrypted", f"{minor}:KRW", key=data_key, key_version=1
        )
        transaction.merchant_encrypted = encrypt_model_field(
            transaction,
            "merchant_encrypted",
            f"merchant-{index % 40}",
            key=data_key,
            key_version=1,
        )
        transaction.save(update_fields=["amount_encrypted", "merchant_encrypted"])
        created.append(transaction)
    return {"account": account, "card": card, "transactions": created, "kinds": kinds}


def expected_bucket_totals(kinds: list[str]) -> dict[str, int]:
    """The totals a person with a calculator would reach, from the mix alone."""

    from apps.transactions.classification import bucket_of

    running = {"spending": 0, "refund": 0, "income": 0, "neutral": 0, "unresolved": 0}
    for index, kind in enumerate(kinds):
        running[bucket_of(kind)] += 1_000 + index
    return running


# ---------------------------------------------------------------------------
# Correctness over encrypted amounts
# ---------------------------------------------------------------------------


def test_a_month_of_encrypted_amounts_totals_correctly(
    owner: Any, data_key: bytes, encrypted_month: dict[str, Any]
) -> None:
    expected = expected_bucket_totals(encrypted_month["kinds"])

    totals = monthly_spending(owner, year=2026, month=8, data_key=data_key).totals("KRW")

    assert totals.gross_spending_minor == expected["spending"]
    assert totals.refunds_minor == expected["refund"]
    assert totals.income_minor == expected["income"]
    assert totals.neutral_minor == expected["neutral"]
    assert totals.unresolved_minor == expected["unresolved"]
    assert totals.transaction_count == FIXTURE_ROWS


def test_every_amount_is_accounted_for_exactly_once(
    owner: Any, data_key: bytes, encrypted_month: dict[str, Any]
) -> None:
    """The buckets must partition the money, not sample it."""

    totals = monthly_spending(owner, year=2026, month=8, data_key=data_key).totals("KRW")
    bucketed = (
        totals.gross_spending_minor
        + totals.refunds_minor
        + totals.income_minor
        + totals.neutral_minor
        + totals.unresolved_minor
    )

    assert bucketed == decrypted_total(encrypted_month["transactions"], data_key=data_key)


def test_the_breakdowns_reconcile_over_encrypted_amounts(
    owner: Any, data_key: bytes, encrypted_month: dict[str, Any]
) -> None:
    month = monthly_spending(owner, year=2026, month=8, data_key=data_key).totals("KRW")

    by_category = category_breakdown(owner, start=AUGUST[0], end=AUGUST[1], data_key=data_key)
    by_merchant = merchant_breakdown(owner, start=AUGUST[0], end=AUGUST[1], data_key=data_key)

    assert reconciles(by_category, month)
    assert reconciles(by_merchant, month)


def test_the_overview_agrees_over_encrypted_amounts(
    owner: Any, data_key: bytes, encrypted_month: dict[str, Any]
) -> None:
    month = monthly_spending(owner, year=2026, month=8, data_key=data_key).totals("KRW")

    overview = period_overview(owner, start=AUGUST[0], end=AUGUST[1], data_key=data_key)

    assert overview.net_spending_minor == month.net_spending_minor
    assert overview.income_minor == month.income_minor
    assert overview.excluded_minor == month.neutral_minor
    assert overview.unresolved_minor == month.unresolved_minor


# ---------------------------------------------------------------------------
# Totals survive a key rotation
# ---------------------------------------------------------------------------


def test_totals_are_unchanged_after_rotating_the_encryption_key(
    owner: Any, data_key: bytes, encrypted_month: dict[str, Any]
) -> None:
    """Re-encrypting under a new key version must not move a single figure."""

    before = monthly_spending(owner, year=2026, month=8, data_key=data_key).totals("KRW")

    rotated = os.urandom(32)
    for transaction in CanonicalTransaction.objects.filter(user=owner):
        for field in ("amount_encrypted", "merchant_encrypted"):
            if not getattr(transaction, field):
                continue
            plaintext = decrypt_model_field(transaction, field, key=data_key)
            setattr(
                transaction,
                field,
                encrypt_model_field(transaction, field, plaintext, key=rotated, key_version=2),
            )
        transaction.save(update_fields=["amount_encrypted", "merchant_encrypted"])

    after = monthly_spending(owner, year=2026, month=8, data_key=rotated).totals("KRW")

    assert after == before


def test_the_old_key_no_longer_reads_a_rotated_month(
    owner: Any, data_key: bytes, encrypted_month: dict[str, Any]
) -> None:
    """A report cannot half-read a rotated month and report a smaller total."""

    from apps.core.crypto import InvalidCiphertextError

    rotated = os.urandom(32)
    for transaction in CanonicalTransaction.objects.filter(user=owner)[:5]:
        plaintext = decrypt_model_field(transaction, "amount_encrypted", key=data_key)
        transaction.amount_encrypted = encrypt_model_field(
            transaction, "amount_encrypted", plaintext, key=rotated, key_version=2
        )
        transaction.save(update_fields=["amount_encrypted"])

    with pytest.raises(InvalidCiphertextError):
        monthly_spending(owner, year=2026, month=8, data_key=data_key).totals("KRW")


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


def test_a_month_of_encrypted_amounts_reports_within_budget(
    owner: Any, data_key: bytes, encrypted_month: dict[str, Any]
) -> None:
    totals, timings = measure_report(owner, start=AUGUST[0], end=AUGUST[1], data_key=data_key)

    assert totals.transaction_count == FIXTURE_ROWS
    assert timings.row_count == FIXTURE_ROWS
    assert timings.within_budget(), timings.exceeded()


def test_the_timings_name_their_own_dominant_cost(
    owner: Any, data_key: bytes, encrypted_month: dict[str, Any]
) -> None:
    """A slow report should say what to fix rather than invite a guess."""

    _, timings = measure_report(owner, start=AUGUST[0], end=AUGUST[1], data_key=data_key)

    assert timings.dominant_cost in {"query", "decrypt", "aggregate"}
    assert timings.total_ms > 0
    assert timings.per_thousand("total_ms") > 0


def test_snapshots_are_not_justified_at_a_personal_volume(
    owner: Any, data_key: bytes, encrypted_month: dict[str, Any]
) -> None:
    """A snapshot is a plaintext total outside the store. It needs a real reason."""

    _, timings = measure_report(owner, start=AUGUST[0], end=AUGUST[1], data_key=data_key)

    assert timings.row_count < SNAPSHOT_REVIEW_THRESHOLD
    assert not timings.snapshots_would_help


def test_a_snapshot_would_only_be_considered_when_decryption_dominates() -> None:
    """Volume alone is not the trigger; the cost has to be in the decryption."""

    heavy_decrypt = ReportTimings(
        row_count=SNAPSHOT_REVIEW_THRESHOLD, query_ms=10, decrypt_ms=5_000, aggregate_ms=20
    )
    heavy_query = ReportTimings(
        row_count=SNAPSHOT_REVIEW_THRESHOLD, query_ms=5_000, decrypt_ms=10, aggregate_ms=20
    )
    small = ReportTimings(row_count=100, query_ms=10, decrypt_ms=5_000, aggregate_ms=20)

    assert heavy_decrypt.snapshots_would_help
    # A slow query is fixed with an index, not with a copy of the data.
    assert not heavy_query.snapshots_would_help
    assert not small.snapshots_would_help


def test_an_empty_period_costs_nothing_and_divides_by_nothing(owner: Any) -> None:
    totals, timings = measure_report(owner, start=AUGUST[0], end=AUGUST[1])

    assert totals.transaction_count == 0
    assert timings.row_count == 0
    assert timings.per_thousand("total_ms") == 0.0
    assert timings.within_budget()


def test_the_budget_names_every_stage() -> None:
    """A stage with no budget is a cost nobody would notice growing."""

    assert set(BUDGET) == {"query_ms", "decrypt_ms", "aggregate_ms", "total_ms"}


def test_exceeding_a_budget_says_which_one_and_by_how_much() -> None:
    timings = ReportTimings(row_count=1_000, query_ms=1, decrypt_ms=9_999, aggregate_ms=1)

    exceeded = timings.exceeded()

    assert not timings.within_budget()
    assert any("decrypt_ms" in message for message in exceeded)
    assert any("9999" in message for message in exceeded)


# ---------------------------------------------------------------------------
# No plaintext cache, anywhere
# ---------------------------------------------------------------------------


def test_no_report_page_writes_a_financial_value_to_the_cache(
    owner: Any, data_key: bytes, encrypted_month: dict[str, Any], monkeypatch: Any
) -> None:
    """Every page, in one test, because one page forgetting is enough."""

    cache.clear()
    writes: list[Any] = []
    for name in ("set", "set_many", "add", "get_or_set"):
        monkeypatch.setattr(cache, name, lambda *args, **kwargs: writes.append(args))
    client = Client()
    client.force_login(owner)

    pages = (
        "report-overview",
        "report-categories",
        "report-merchants",
        "report-accounts",
        "report-cards",
        "report-outstanding",
    )
    for page in pages:
        response = client.get(reverse(page), {"year": "2026", "month": "8"})
        assert response.status_code == 200, page
        assert "no-store" in response.headers["Cache-Control"], page

    assert writes == []


def test_the_aggregation_functions_write_nothing_to_the_cache(
    owner: Any, data_key: bytes, encrypted_month: dict[str, Any], monkeypatch: Any
) -> None:
    """Below the views too: the services must not be the ones caching."""

    cache.clear()
    writes: list[Any] = []
    monkeypatch.setattr(cache, "set", lambda *args, **kwargs: writes.append(args))

    monthly_spending(owner, year=2026, month=8, data_key=data_key)
    category_breakdown(owner, start=AUGUST[0], end=AUGUST[1], data_key=data_key)
    merchant_breakdown(owner, start=AUGUST[0], end=AUGUST[1], data_key=data_key)
    period_overview(owner, start=AUGUST[0], end=AUGUST[1], data_key=data_key)

    assert writes == []
