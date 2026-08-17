"""One screenshot, all the way to a report (#98, specification 31).

Every other test in this suite checks one stage. This one checks that the stages
fit together: a parsed row becomes an observation, a reviewer accepts it, the
ledger balances, the month adds up, the export contains it, and a backup restores
it. Each seam here has been a place where two correct halves disagreed.

The suite is otherwise a collection of unit tests, and a collection of unit tests
can pass while the product does not work.
"""

from __future__ import annotations

import base64
import csv
import io
import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.core.backup import create_backup, unpack_backup
from apps.core.key_management import get_user_data_key, provision_user_data_key
from apps.core.models import AuditEvent
from apps.ledger.models import LedgerEntry
from apps.ledger.posting import entry_amount
from apps.observations.models import ImportedObservation
from apps.observations.queue import review_queue
from apps.observations.review import accept_observation, correct_observation
from apps.observations.services import import_parser_selection
from apps.parsing.contracts import (
    ParsedObservation,
    ParserMetadata,
    ParserSupport,
    TransactionDirection,
)
from apps.parsing.registry import ParserSelection
from apps.reports.breakdown import category_breakdown, reconciles
from apps.reports.exports import safe_export_path
from apps.reports.models import TransactionExport
from apps.reports.overview import period_overview
from apps.reports.services import create_export
from apps.reports.spending import month_bounds, monthly_spending
from apps.reports.workload import outstanding_work
from apps.transactions.models import CanonicalTransaction
from apps.transactions.money import read_money
from tests.factories import (
    make_account,
    make_document,
    make_ledger_accounts,
    make_ocr_run,
    make_user,
)

pytestmark = pytest.mark.django_db

AUGUST = month_bounds(2026, 8)
COFFEE = "스타벅스 강남점"
GROCERIES = "이마트 성수점"


@pytest.fixture
def master_key(tmp_path: Path, settings: Any) -> bytes:
    key = os.urandom(32)
    path = tmp_path / "master.key"
    path.write_text(base64.urlsafe_b64encode(key).decode(), encoding="ascii")
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)
    settings.DOCUMENT_TMP_ROOT = str(tmp_path / "documents")
    settings.EXPORT_TMP_ROOT = str(tmp_path / "exports")
    return key


@pytest.fixture
def owner(master_key: bytes) -> Any:
    user = make_user(email="end-to-end@example.com")
    provision_user_data_key(user=user, actor=user, master_key=master_key)
    user.last_login = timezone.now()
    user.save(update_fields=["last_login"])
    return user


@pytest.fixture
def data_key(owner: Any, master_key: bytes) -> bytes:
    return get_user_data_key(user=owner, actor=owner, master_key=master_key)


def parsed(merchant: str, amount: str, day: int, **overrides: Any) -> ParsedObservation:
    values: dict[str, Any] = {
        "occurred_on": date(2026, 8, day),
        "amount": Decimal(amount),
        "currency": "KRW",
        "direction": TransactionDirection.DEBIT,
        "merchant": merchant,
        "confidence_factors": {"token_confidence": 0.95, "amount_confidence": 0.95},
        "parser_name": "toss_bank",
        "parser_version": "1.0",
        "parser_support_score": 0.95,
    }
    values.update(overrides)
    return ParsedObservation(**values)


def test_a_screenshot_becomes_a_report_a_ledger_and_a_backup(
    owner: Any, data_key: bytes, master_key: bytes, tmp_path: Path
) -> None:
    """The whole path, in the order a person walks it."""

    account = make_account(owner, name_encrypted="checking", name_blind_index="e2e-checking")
    ledger = make_ledger_accounts(owner, account, prefix="e2e")

    # 1. A screenshot is parsed into candidate rows. Nothing is history yet.
    document = make_document(owner, file_sha256="e" * 64)
    run = make_ocr_run(owner, document)
    rows = import_parser_selection(
        document=document,
        ocr_run=run,
        selection=ParserSelection(
            ParserMetadata("toss_bank", "1.0"),
            ParserSupport(0.95, "bank_transaction_list", ()),
            (
                parsed(COFFEE, "4200", 3),
                parsed(GROCERIES, "42900", 15),
                parsed("월급", "3000000", 25, direction=TransactionDirection.CREDIT),
            ),
        ),
        data_key=data_key,
        key_version=1,
    ).observations

    assert len(rows) == 3
    assert not CanonicalTransaction.objects.filter(user=owner).exists()

    # 2. The review queue shows them, and the outstanding-work report agrees.
    page = review_queue(owner)
    workload = outstanding_work(owner)
    assert {item.observation.pk for item in page.items} == {row.pk for row in rows}
    assert workload.unreviewed_count == 3
    assert not workload.is_clear

    # 3. A reviewer corrects one row, then accepts all three.
    correct_observation(
        rows[0].pk,
        user=owner,
        data_key=data_key,
        key_version=1,
        corrections={"amount_minor": 4500},
    )
    accepted = []
    for row, kind in (
        (rows[0], CanonicalTransaction.TransactionType.PURCHASE),
        (rows[1], CanonicalTransaction.TransactionType.PURCHASE),
        (rows[2], CanonicalTransaction.TransactionType.INCOME),
    ):
        accepted.append(
            accept_observation(
                row.pk,
                user=owner,
                data_key=data_key,
                financial_account=account,
                transaction_type=kind,
                ledger_accounts=ledger,
            )
        )

    # 4. The ledger balances, and its amounts are encrypted like everything else.
    entries = LedgerEntry.objects.filter(transaction__user=owner)
    assert entries.count() == 6
    for entry in entries:
        assert "4500" not in entry.amount_encrypted
        assert entry_amount(entry, data_key=data_key).amount_minor > 0

    # 5. The month adds up, and the correction is the figure that counts.
    totals = monthly_spending(owner, year=2026, month=8, data_key=data_key).totals("KRW")
    assert totals.gross_spending_minor == 4_500 + 42_900
    assert totals.income_minor == 3_000_000
    assert totals.net_position_minor == 3_000_000 - 47_400

    # 6. The breakdown reconciles with the month it describes.
    breakdown = category_breakdown(owner, start=AUGUST[0], end=AUGUST[1], data_key=data_key)
    assert reconciles(breakdown, totals)
    # Nothing has been categorised, so it is all on the unassigned line.
    assert breakdown.unassigned is not None
    assert breakdown.unassigned.net_spending_minor == 47_400

    # 7. Income and spending are kept apart on the overview.
    overview = period_overview(owner, start=AUGUST[0], end=AUGUST[1], data_key=data_key)
    assert overview.income_minor == 3_000_000
    assert overview.net_spending_minor == 47_400
    assert overview.excluded_minor == 0

    # 8. The queue is clear and the workload report says so.
    assert outstanding_work(owner).unreviewed_count == 0

    # 9. An export carries the amounts as integers and the merchants readably.
    export = create_export(
        user=owner, export_format=TransactionExport.Format.CSV, data_key=data_key
    )
    exported = list(
        csv.DictReader(io.StringIO(safe_export_path(f"{export.pk}.csv").read_bytes().decode()))
    )
    assert {row["amount_minor"] for row in exported} == {"4500", "42900", "3000000"}
    assert {row["merchant"] for row in exported} == {COFFEE, GROCERIES, "월급"}

    # 10. A backup restores to the same figures, with the key kept separately.
    archive = tmp_path / "backup.enc"
    create_backup(archive, passphrase="an end to end passphrase")
    report = unpack_backup(
        archive, passphrase="an end to end passphrase", destination=tmp_path / "restored"
    )
    # Ledger entries protect their transaction, so they go first — the same
    # order a real restore into a wiped database has to use.
    LedgerEntry.objects.all().delete()
    CanonicalTransaction.objects.all().delete()
    call_command("loaddata", str(report.database_path), verbosity=0)

    restored = monthly_spending(owner, year=2026, month=8, data_key=data_key).totals("KRW")
    assert restored == totals
    assert (
        read_money(
            CanonicalTransaction.objects.get(pk=accepted[0].pk),
            "amount_encrypted",
            data_key=data_key,
        ).amount_minor
        == 4_500
    )

    # 11. Every decision left an audit trail.
    events = set(AuditEvent.objects.filter(user=owner).values_list("event_type", flat=True))
    assert {"observation_corrected", "observation_accepted", "export_created"} <= events


def test_a_rejected_row_never_reaches_the_books(owner: Any, data_key: bytes) -> None:
    """The separation the whole two-stage design exists for."""

    from apps.observations.review import reject_observation

    document = make_document(owner, file_sha256="f" * 64)
    run = make_ocr_run(owner, document)
    row = import_parser_selection(
        document=document,
        ocr_run=run,
        selection=ParserSelection(
            ParserMetadata("toss_bank", "1.0"),
            ParserSupport(0.95, "bank_transaction_list", ()),
            (parsed(COFFEE, "4200", 3),),
        ),
        data_key=data_key,
        key_version=1,
    ).observations[0]

    reject_observation(row.pk, user=owner, reason="not mine")

    row.refresh_from_db()
    assert row.review_status == ImportedObservation.ReviewStatus.REJECTED
    assert (
        monthly_spending(owner, year=2026, month=8, data_key=data_key)
        .totals("KRW")
        .net_spending_minor
        == 0
    )
    # Visible as a decision, counted in nothing.
    statuses = {item.key: item.count for item in outstanding_work(owner).review_statuses}
    assert statuses["rejected"] == 1


def test_the_web_path_from_review_to_report(owner: Any, data_key: bytes) -> None:
    """The pages a person actually clicks, not the services beneath them."""

    account = make_account(owner, name_encrypted="checking", name_blind_index="e2e-web")
    document = make_document(owner, file_sha256="a" * 64)
    run = make_ocr_run(owner, document)
    row = import_parser_selection(
        document=document,
        ocr_run=run,
        selection=ParserSelection(
            ParserMetadata("toss_bank", "1.0"),
            ParserSupport(0.95, "bank_transaction_list", ()),
            (parsed(GROCERIES, "42900", 15),),
        ),
        data_key=data_key,
        key_version=1,
    ).observations[0]
    client = Client()
    client.force_login(owner)

    queue = client.get(reverse("review-queue"))
    accepted = client.post(
        reverse("observation-action", kwargs={"pk": row.pk, "action": "accept"}),
        data={"financial_account": str(account.pk), "transaction_type": "purchase"},
        follow=True,
    )
    report = client.get(reverse("report-overview"), {"year": "2026", "month": "8"})
    outstanding = client.get(reverse("report-outstanding"))

    assert queue.status_code == 200
    assert accepted.status_code == 200
    assert report.status_code == 200
    assert report.context["overview"].net_spending_minor == 42_900
    assert outstanding.context["workload"].unreviewed_count == 0
