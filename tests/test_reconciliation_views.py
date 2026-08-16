import base64
import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.core.key_management import get_user_data_key, provision_user_data_key
from apps.observations.models import ImportedObservation
from apps.observations.services import import_parser_selection
from apps.parsing.contracts import (
    ParsedObservation,
    ParserMetadata,
    ParserSupport,
    TransactionDirection,
)
from apps.parsing.registry import ParserSelection
from apps.reconciliation.models import ReconciliationMatch
from apps.reconciliation.services import record_match
from tests.factories import make_document, make_ocr_run, make_user

pytestmark = pytest.mark.django_db


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
    user = make_user(email="match-view-owner@example.com")
    provision_user_data_key(user=user, actor=user, master_key=master_key)
    return user


@pytest.fixture
def client_for(owner: Any) -> Client:
    client = Client()
    client.force_login(owner)
    return client


def parsed(**overrides: Any) -> ParsedObservation:
    values: dict[str, Any] = {
        "occurred_on": date(2026, 8, 15),
        "amount": Decimal("4200"),
        "currency": "KRW",
        "direction": TransactionDirection.DEBIT,
        "merchant": "스타벅스",
        "confidence_factors": {"token_confidence": 0.95, "amount_confidence": 0.95},
        "parser_name": "toss_bank",
        "parser_version": "1.0",
        "parser_support_score": 0.95,
    }
    values.update(overrides)
    return ParsedObservation(**values)


def seed(user: Any, master_key: bytes, count: int = 2) -> Any:
    document = make_document(user)
    run = make_ocr_run(user, document)
    data_key = get_user_data_key(user=user, actor=user, master_key=master_key)
    return import_parser_selection(
        document=document,
        ocr_run=run,
        selection=ParserSelection(
            ParserMetadata("toss_bank", "1.0"),
            ParserSupport(0.95, "bank_transaction_list", ()),
            tuple(parsed(merchant=f"row-{index}") for index in range(count)),
        ),
        data_key=data_key,
        key_version=1,
    ).observations


def duplicate_match(user: Any, rows: Any, score: int = 95) -> ReconciliationMatch:
    return record_match(
        user=user,
        left_observation_id=rows[0].pk,
        right_observation_id=rows[1].pk,
        match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
        score=score,
    )


def test_the_match_queue_lists_open_candidates(
    owner: Any, master_key: bytes, client_for: Client
) -> None:
    rows = seed(owner, master_key)
    duplicate_match(owner, rows)

    response = client_for.get(reverse("match-queue"))

    assert response.status_code == 200
    assert len(response.context["matches"]) == 1


def test_the_match_queue_never_shows_another_users_candidates(
    owner: Any, master_key: bytes
) -> None:
    rows = seed(owner, master_key)
    duplicate_match(owner, rows)
    intruder = make_user(email="match-view-intruder@example.com")
    provision_user_data_key(user=intruder, actor=intruder, master_key=master_key)
    client = Client()
    client.force_login(intruder)

    response = client.get(reverse("match-queue"))

    assert len(response.context["matches"]) == 0


def test_the_detail_page_shows_both_rows_side_by_side(
    owner: Any, master_key: bytes, client_for: Client
) -> None:
    rows = seed(owner, master_key)
    match = duplicate_match(owner, rows)

    response = client_for.get(reverse("match-detail", kwargs={"pk": match.pk}))

    assert response.status_code == 200
    candidates = response.context["candidates"]
    assert len(candidates) == 2
    assert {item["values"].merchant for item in candidates} == {"row-0", "row-1"}
    assert response.context["is_duplicate"] is True
    # A duplicate merge must make the reviewer pick the surviving row.
    assert 'name="winner"' in response.content.decode()


def test_merging_requires_choosing_a_winner(
    owner: Any, master_key: bytes, client_for: Client
) -> None:
    rows = seed(owner, master_key)
    match = duplicate_match(owner, rows)

    client_for.post(
        reverse("match-action", kwargs={"pk": match.pk, "action": "confirm"}), follow=True
    )

    match.refresh_from_db()
    assert match.status == ReconciliationMatch.Status.PROPOSED
    for row in rows:
        row.refresh_from_db()
        assert row.review_status == ImportedObservation.ReviewStatus.UNREVIEWED


def test_choosing_a_winner_merges_the_pair(
    owner: Any, master_key: bytes, client_for: Client
) -> None:
    rows = seed(owner, master_key)
    match = duplicate_match(owner, rows)
    winner_id = match.left_observation_id

    client_for.post(
        reverse("match-action", kwargs={"pk": match.pk, "action": "confirm"}),
        data={"winner": str(winner_id)},
        follow=True,
    )

    match.refresh_from_db()
    loser = ImportedObservation.objects.get(pk=match.right_observation_id)
    assert match.status == ReconciliationMatch.Status.CONFIRMED
    assert loser.review_status == ImportedObservation.ReviewStatus.MERGED
    assert loser.merged_into_id == winner_id


def test_dismissing_a_candidate_leaves_both_rows_alone(
    owner: Any, master_key: bytes, client_for: Client
) -> None:
    rows = seed(owner, master_key)
    match = duplicate_match(owner, rows)

    client_for.post(
        reverse("match-action", kwargs={"pk": match.pk, "action": "reject"}), follow=True
    )

    match.refresh_from_db()
    assert match.status == ReconciliationMatch.Status.REJECTED
    for row in rows:
        row.refresh_from_db()
        assert row.review_status == ImportedObservation.ReviewStatus.UNREVIEWED


def test_a_settlement_candidate_confirms_without_merging(
    owner: Any, master_key: bytes, client_for: Client
) -> None:
    rows = seed(owner, master_key)
    match = record_match(
        user=owner,
        left_observation_id=rows[0].pk,
        right_observation_id=rows[1].pk,
        match_type=ReconciliationMatch.MatchType.CREDIT_CARD_PAYMENT,
        score=90,
    )

    client_for.post(
        reverse("match-action", kwargs={"pk": match.pk, "action": "confirm"}), follow=True
    )

    match.refresh_from_db()
    assert match.status == ReconciliationMatch.Status.CONFIRMED
    for row in rows:
        row.refresh_from_db()
        assert row.review_status == ImportedObservation.ReviewStatus.UNREVIEWED


def test_another_users_candidate_is_not_found(owner: Any, master_key: bytes) -> None:
    rows = seed(owner, master_key)
    match = duplicate_match(owner, rows)
    intruder = make_user(email="match-action-intruder@example.com")
    provision_user_data_key(user=intruder, actor=intruder, master_key=master_key)
    client = Client()
    client.force_login(intruder)

    assert client.get(reverse("match-detail", kwargs={"pk": match.pk})).status_code == 404
    assert (
        client.post(
            reverse("match-action", kwargs={"pk": match.pk, "action": "reject"})
        ).status_code
        == 404
    )


def test_an_unknown_match_action_is_not_found(
    owner: Any, master_key: bytes, client_for: Client
) -> None:
    rows = seed(owner, master_key)
    match = duplicate_match(owner, rows)

    response = client_for.post(
        reverse("match-action", kwargs={"pk": match.pk, "action": "annihilate"})
    )

    assert response.status_code == 404


def test_duplicate_candidates_raise_their_rows_in_the_review_queue(
    owner: Any, master_key: bytes, client_for: Client
) -> None:
    rows = seed(owner, master_key, count=3)
    record_match(
        user=owner,
        left_observation_id=rows[1].pk,
        right_observation_id=rows[2].pk,
        match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
        score=95,
    )

    response = client_for.get(reverse("review-queue"))
    page = response.context["page"]

    assert page.counts["duplicate"] == 2
    assert any("duplicate_candidate" in item.reasons for item in page.items)
