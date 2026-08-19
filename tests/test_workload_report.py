"""What is still waiting for a decision (#88, specification 19, 25.3).

A month's total is only as trustworthy as the pile of unreviewed screenshots
behind it. These tests hold the page to counting that pile accurately, to linking
every count to somewhere a user can act, and to never counting anybody else's.
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

from apps.core.key_management import provision_user_data_key
from apps.observations.models import ImportedObservation
from apps.observations.queue import QueueFilter, queue_counts
from apps.reconciliation.models import ReconciliationMatch
from apps.reconciliation.services import queue_match_ids, record_match, reject_match
from apps.reports.workload import HIGH_CONFIDENCE_THRESHOLD, outstanding_work
from apps.transactions.models import CanonicalTransaction
from tests.factories import make_document, make_ocr_run, make_user

pytestmark = pytest.mark.django_db

_Status = ImportedObservation.ReviewStatus


@pytest.fixture
def master_key(tmp_path: Path, settings: Any) -> bytes:
    key = os.urandom(32)
    path = tmp_path / "master.key"
    path.write_text(base64.urlsafe_b64encode(key).decode(), encoding="ascii")
    path.chmod(0o600)
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)
    settings.DOCUMENT_TMP_ROOT = str(tmp_path / "documents")
    return key


@pytest.fixture
def owner(master_key: bytes) -> Any:
    user = make_user(email="workload-owner@example.com")
    provision_user_data_key(user=user, actor=user, master_key=master_key)
    return user


def make_row(
    user: Any,
    *,
    document: Any = None,
    review_status: str = _Status.UNREVIEWED,
    confidence: float = 0.95,
    risk_score: int = 0,
    amount_uncertain: bool = False,
    **overrides: Any,
) -> ImportedObservation:
    stored = document or make_document(user, file_sha256=os.urandom(32).hex())
    values: dict[str, Any] = {
        "user": user,
        "source_document": stored,
        "ocr_run": make_ocr_run(user, stored),
        "occurred_at": date(2026, 8, 15),
        "currency": "KRW",
        "direction": ImportedObservation.Direction.DEBIT,
        "review_status": review_status,
        "overall_confidence": confidence,
        "risk_score": risk_score,
        "amount_uncertain": amount_uncertain,
    }
    values.update(overrides)
    return ImportedObservation.objects.create(**values)


def by_key(items: Any) -> dict[str, Any]:
    return {item.key: item for item in items}


# ---------------------------------------------------------------------------
# Review status
# ---------------------------------------------------------------------------


def test_rows_are_counted_by_review_status(owner: Any) -> None:
    make_row(owner)
    make_row(owner)
    make_row(owner, review_status=_Status.ACCEPTED)
    make_row(owner, review_status=_Status.REJECTED)

    statuses = by_key(outstanding_work(owner).review_statuses)

    assert statuses["unreviewed"].count == 2
    assert statuses["accepted"].count == 1
    assert statuses["rejected"].count == 1
    assert statuses["merged"].count == 0


def test_a_rejected_row_stays_visible_and_says_why(owner: Any) -> None:
    """It is a decision the user made, not something to hide."""

    make_row(owner, review_status=_Status.REJECTED)

    rejected = by_key(outstanding_work(owner).review_statuses)["rejected"]

    assert rejected.count == 1
    assert rejected.note


def test_a_rejected_row_never_reaches_a_confirmed_total(owner: Any) -> None:
    """Visible here as a decision; absent from every figure."""

    from apps.reports.spending import monthly_spending
    from tests.factories import make_account

    account = make_account(owner, name_blind_index="workload-account")
    document = make_document(owner, file_sha256="7" * 64)
    accepted = make_row(owner, document=document, review_status=_Status.ACCEPTED)
    accepted.canonical_transaction = CanonicalTransaction.objects.create(
        user=owner,
        created_by=owner,
        financial_account=account,
        occurred_at=date(2026, 8, 15),
        amount_encrypted="42900:KRW",
        currency="KRW",
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
    )
    accepted.save(update_fields=["canonical_transaction"])
    make_row(owner, document=document, review_status=_Status.REJECTED)

    workload = outstanding_work(owner)
    month = monthly_spending(owner, year=2026, month=8).totals("KRW")

    # The rejection is counted here...
    assert by_key(workload.review_statuses)["rejected"].count == 1
    # ...and the month reports only the accepted row's amount.
    assert month.net_spending_minor == 42_900
    assert month.transaction_count == 1


def test_the_workload_is_clear_when_nothing_waits(owner: Any) -> None:
    make_row(owner, review_status=_Status.ACCEPTED)

    assert outstanding_work(owner).is_clear


def test_the_workload_is_not_clear_while_a_row_waits(owner: Any) -> None:
    make_row(owner)

    workload = outstanding_work(owner)

    assert not workload.is_clear
    assert workload.unreviewed_count == 1


# ---------------------------------------------------------------------------
# Confidence bands
# ---------------------------------------------------------------------------


def test_open_rows_are_banded_by_confidence(owner: Any) -> None:
    """One row at 0.2 among fifty at 0.98 is the row that matters."""

    make_row(owner, confidence=0.98)
    make_row(owner, confidence=0.80)
    make_row(owner, confidence=0.20)

    bands = by_key(outstanding_work(owner).confidence_bands)

    assert bands["high"].count == 1
    assert bands["medium"].count == 1
    assert bands["low"].count == 1
    assert f"{HIGH_CONFIDENCE_THRESHOLD:.0%}" in bands["high"].label


def test_high_risk_rows_are_counted_and_explained(owner: Any) -> None:
    make_row(owner, risk_score=100, amount_uncertain=True)

    risky = by_key(outstanding_work(owner).confidence_bands)["risky"]

    assert risky.count == 1
    assert risky.note


def test_decided_rows_are_not_banded(owner: Any) -> None:
    """Confidence matters for work outstanding, not for work finished."""

    make_row(owner, review_status=_Status.ACCEPTED, confidence=0.2)

    assert all(item.count == 0 for item in outstanding_work(owner).confidence_bands)


def test_the_low_confidence_band_links_to_its_queue_filter(owner: Any) -> None:
    make_row(owner, confidence=0.2)

    low = by_key(outstanding_work(owner).confidence_bands)["low"]

    assert low.query == f"filter={QueueFilter.LOW_CONFIDENCE.value}"


# ---------------------------------------------------------------------------
# Queue filters and reconciliation
# ---------------------------------------------------------------------------


def test_queue_filter_counts_come_from_the_queue_itself(owner: Any) -> None:
    """The page and the queue can never disagree about how many rows wait."""

    make_row(owner, confidence=0.2)
    make_row(owner, amount_uncertain=True)

    filters = by_key(outstanding_work(owner).queue_filters)
    direct = queue_counts(owner)

    assert {name: item.count for name, item in filters.items()} == {
        name.value: direct[name.value] for name in QueueFilter
    }


def test_every_queue_filter_is_offered_a_link(owner: Any) -> None:
    make_row(owner)

    for item in outstanding_work(owner).queue_filters:
        assert item.query
        assert item.url_name == "review-queue"


def test_reconciliation_candidates_are_counted_by_status(owner: Any) -> None:
    document = make_document(owner, file_sha256="c" * 64)
    rows = [make_row(owner, document=document) for _ in range(4)]
    open_match = record_match(
        user=owner,
        left_observation_id=rows[0].pk,
        right_observation_id=rows[1].pk,
        match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
        score=95,
    )
    dismissed = record_match(
        user=owner,
        left_observation_id=rows[2].pk,
        right_observation_id=rows[3].pk,
        match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
        score=70,
    )
    reject_match(dismissed.pk, user=owner)

    reconciliation = by_key(outstanding_work(owner).reconciliation)

    assert open_match.status == ReconciliationMatch.Status.PROPOSED
    assert reconciliation["proposed"].count == 1
    assert reconciliation["rejected"].count == 1
    assert reconciliation["rejected"].note
    assert reconciliation["proposed"].url_name == "match-queue"


def test_an_open_candidate_keeps_the_workload_from_being_clear(owner: Any) -> None:
    document = make_document(owner, file_sha256="d" * 64)
    rows = [make_row(owner, document=document, review_status=_Status.ACCEPTED) for _ in range(2)]
    record_match(
        user=owner,
        left_observation_id=rows[0].pk,
        right_observation_id=rows[1].pk,
        match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
        score=95,
    )

    workload = outstanding_work(owner)

    assert workload.unreviewed_count == 0
    assert workload.open_match_count == 1
    assert not workload.is_clear


def test_candidate_derived_filters_are_reflected_when_ids_are_supplied(owner: Any) -> None:
    document = make_document(owner, file_sha256="e" * 64)
    rows = [make_row(owner, document=document) for _ in range(2)]
    record_match(
        user=owner,
        left_observation_id=rows[0].pk,
        right_observation_id=rows[1].pk,
        match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
        score=95,
    )

    workload = outstanding_work(owner, match_ids=queue_match_ids(owner))

    assert by_key(workload.queue_filters)["duplicate"].count == 2


# ---------------------------------------------------------------------------
# Per screenshot
# ---------------------------------------------------------------------------


def test_screenshots_are_listed_with_their_progress(owner: Any) -> None:
    document = make_document(owner, file_sha256="f" * 64)
    make_row(owner, document=document)
    make_row(owner, document=document, review_status=_Status.ACCEPTED)
    make_row(owner, document=document, review_status=_Status.REJECTED)

    backlog = outstanding_work(owner).documents[0]

    assert backlog.document_id == document.pk
    assert backlog.total == 3
    assert backlog.unreviewed == 1
    assert backlog.accepted == 1
    assert backlog.rejected == 1
    assert backlog.decided == 2
    assert not backlog.is_complete


def test_a_corrected_row_counts_as_accepted(owner: Any) -> None:
    document = make_document(owner, file_sha256="1" * 64)
    make_row(owner, document=document, review_status=_Status.CORRECTED)

    backlog = outstanding_work(owner).documents[0]

    assert backlog.accepted == 1
    assert backlog.is_complete


def test_incomplete_screenshots_are_listed_first(owner: Any) -> None:
    done = make_document(owner, file_sha256="2" * 64)
    make_row(owner, document=done, review_status=_Status.ACCEPTED)
    waiting = make_document(owner, file_sha256="3" * 64)
    make_row(owner, document=waiting)

    documents = outstanding_work(owner).documents

    assert documents[0].document_id == waiting.pk
    assert [item.document_id for item in outstanding_work(owner).incomplete_documents] == [
        waiting.pk
    ]


def test_a_screenshot_with_no_rows_is_not_listed(owner: Any) -> None:
    """Nothing was parsed from it, so there is nothing to review."""

    make_document(owner, file_sha256="4" * 64)

    assert outstanding_work(owner).documents == ()


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


def test_another_users_work_is_never_counted(owner: Any, master_key: bytes) -> None:
    stranger = make_user(email="workload-stranger@example.com")
    provision_user_data_key(user=stranger, actor=stranger, master_key=master_key)
    theirs = make_document(stranger, file_sha256="5" * 64)
    make_row(stranger, document=theirs)
    make_row(stranger, document=theirs)

    workload = outstanding_work(owner)

    assert workload.unreviewed_count == 0
    assert workload.documents == ()
    assert workload.is_clear


def test_another_users_documents_never_appear_on_the_page(owner: Any, master_key: bytes) -> None:
    stranger = make_user(email="workload-onlooker@example.com")
    provision_user_data_key(user=stranger, actor=stranger, master_key=master_key)
    theirs = make_document(stranger, file_sha256="6" * 64)
    make_row(stranger, document=theirs)
    client = Client()
    client.force_login(owner)

    response = client.get(reverse("report-outstanding"))

    assert str(theirs.pk) not in response.content.decode()


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


def test_the_page_links_every_actionable_count(owner: Any) -> None:
    """A number a user cannot act on tells them they have work, not where."""

    make_row(owner, confidence=0.2)
    client = Client()
    client.force_login(owner)

    response = client.get(reverse("report-outstanding"))
    body = response.content.decode()

    assert response.status_code == 200
    assert reverse("review-queue") in body
    assert f"filter={QueueFilter.LOW_CONFIDENCE.value}" in body


def test_the_page_says_so_when_nothing_waits(owner: Any) -> None:
    make_row(owner, review_status=_Status.ACCEPTED)
    client = Client()
    client.force_login(owner)

    response = client.get(reverse("report-outstanding"))

    assert response.context["workload"].is_clear
    assert "Nothing is waiting" in response.content.decode()


def test_the_page_writes_nothing_to_the_cache(owner: Any, monkeypatch: Any) -> None:
    make_row(owner)
    cache.clear()
    writes: list[Any] = []
    monkeypatch.setattr(cache, "set", lambda *args, **kwargs: writes.append(args))
    client = Client()
    client.force_login(owner)

    response = client.get(reverse("report-outstanding"))

    assert response.status_code == 200
    assert writes == []
    assert "no-store" in response.headers["Cache-Control"]


def test_the_page_requires_a_login() -> None:
    response = Client().get(reverse("report-outstanding"))

    assert response.status_code == 302
    assert reverse("login") in response.headers["Location"]
