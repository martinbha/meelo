"""The reconciliation queue: what it proposes, why, and what a person can do (#78).

A score on its own is not a reason. These tests hold the queue to showing the
evidence behind every proposal, to naming what confirming one would produce, and
to letting a reviewer record a relationship the matcher missed.
"""

from __future__ import annotations

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
from apps.reconciliation.explanations import (
    FEATURE_LABELS,
    describe_features,
    proposed_transaction_type,
    proposed_transaction_type_label,
)
from apps.reconciliation.models import ReconciliationMatch
from apps.reconciliation.services import (
    MANUAL_LINK_SCORE,
    ReconciliationError,
    decrypt_match_features,
    link_observations,
    record_match,
    reject_match,
)
from apps.transactions.models import CanonicalTransaction
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
    user = make_user(email="queue-owner@example.com")
    provision_user_data_key(user=user, actor=user, master_key=master_key)
    return user


@pytest.fixture
def data_key(owner: Any, master_key: bytes) -> bytes:
    return get_user_data_key(user=owner, actor=owner, master_key=master_key)


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


def seed(user: Any, key: bytes, count: int = 2) -> Any:
    document = make_document(user)
    run = make_ocr_run(user, document)
    return import_parser_selection(
        document=document,
        ocr_run=run,
        selection=ParserSelection(
            ParserMetadata("toss_bank", "1.0"),
            ParserSupport(0.95, "bank_transaction_list", ()),
            tuple(parsed(merchant=f"row-{index}") for index in range(count)),
        ),
        data_key=key,
        key_version=1,
    ).observations


# ---------------------------------------------------------------------------
# Every proposal shows why it was made
# ---------------------------------------------------------------------------


def test_stored_evidence_is_read_back_for_its_owner(owner: Any, data_key: bytes) -> None:
    rows = seed(owner, data_key)
    match = record_match(
        user=owner,
        left_observation_id=rows[0].pk,
        right_observation_id=rows[1].pk,
        match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
        score=95,
        features=("exact_amount", "same_approval_code"),
        data_key=data_key,
    )

    assert set(decrypt_match_features(match, data_key=data_key)) == {
        "exact_amount",
        "same_approval_code",
    }


def test_a_candidate_stored_without_evidence_shows_no_reasons(owner: Any, data_key: bytes) -> None:
    """A missing key must leave the queue usable, not break it."""

    rows = seed(owner, data_key)
    match = record_match(
        user=owner,
        left_observation_id=rows[0].pk,
        right_observation_id=rows[1].pk,
        match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
        score=95,
    )

    assert decrypt_match_features(match, data_key=data_key) == ()


def emitted_feature_names() -> set[str]:
    """Every feature literal the scoring modules can put on a proposal.

    Read from the source rather than listed by hand, so a new signal added to a
    matcher fails this test instead of quietly reaching a reviewer as a bare
    identifier they cannot check.
    """

    import ast
    import inspect

    from apps.reconciliation import duplicates, matching

    names: set[str] = set()
    for module in (matching, duplicates):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            # features.append("same_date")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "features"
            ):
                names.update(
                    argument.value
                    for argument in node.args
                    if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
                )
            # features = ["exact_amount", "nearby_date"]
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.List):
                targets = {target.id for target in node.targets if isinstance(target, ast.Name)}
                if "features" in targets:
                    names.update(
                        element.value
                        for element in node.value.elts
                        if isinstance(element, ast.Constant) and isinstance(element.value, str)
                    )
    return names


def test_every_feature_the_matchers_emit_has_a_sentence() -> None:
    """A reason shown as a bare identifier is a reason nobody can check."""

    emitted = emitted_feature_names()

    assert emitted, "no feature literals were found; the scraper needs updating"
    assert emitted <= set(FEATURE_LABELS), sorted(emitted - set(FEATURE_LABELS))


def test_an_unknown_feature_is_shown_rather_than_dropped() -> None:
    """Showing fewer reasons than were scored would misrepresent the evidence."""

    described = describe_features(("exact_amount", "something_new"))

    assert described == (FEATURE_LABELS["exact_amount"], "something_new")


def test_the_queue_page_shows_reasons_and_the_proposed_type(
    owner: Any, data_key: bytes, client_for: Client
) -> None:
    rows = seed(owner, data_key)
    record_match(
        user=owner,
        left_observation_id=rows[0].pk,
        right_observation_id=rows[1].pk,
        match_type=ReconciliationMatch.MatchType.INTERNAL_TRANSFER,
        score=85,
        features=("exact_amount", "both_accounts_owned"),
        data_key=data_key,
    )

    response = client_for.get(reverse("match-queue"))
    body = response.content.decode()

    assert response.status_code == 200
    assert FEATURE_LABELS["exact_amount"] in body
    assert FEATURE_LABELS["both_accounts_owned"] in body
    assert "Internal transfer" in body


def test_the_detail_page_explains_the_pairing(
    owner: Any, data_key: bytes, client_for: Client
) -> None:
    rows = seed(owner, data_key)
    match = record_match(
        user=owner,
        left_observation_id=rows[0].pk,
        right_observation_id=rows[1].pk,
        match_type=ReconciliationMatch.MatchType.REFUND_MATCH,
        score=80,
        features=("refund_after_purchase", "similar_merchant"),
        data_key=data_key,
    )

    response = client_for.get(reverse("match-detail", kwargs={"pk": match.pk}))
    body = response.content.decode()

    assert response.status_code == 200
    assert FEATURE_LABELS["refund_after_purchase"] in body
    assert response.context["proposed_type"] == "Refund"


# ---------------------------------------------------------------------------
# The proposed transaction type
# ---------------------------------------------------------------------------


def test_each_relationship_names_what_confirming_would_produce() -> None:
    types = CanonicalTransaction.TransactionType
    matches = ReconciliationMatch.MatchType

    assert proposed_transaction_type(matches.INTERNAL_TRANSFER) == types.INTERNAL_TRANSFER
    assert proposed_transaction_type(matches.REFUND_MATCH) == types.REFUND
    assert proposed_transaction_type(matches.CREDIT_CARD_PAYMENT) == types.CREDIT_CARD_PAYMENT
    assert proposed_transaction_type_label(matches.REFUND_MATCH) == "Refund"


def test_a_duplicate_proposes_no_transaction_type() -> None:
    """Merging two views of one event does not decide what the event was."""

    duplicate = ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION

    assert proposed_transaction_type(duplicate) is None
    assert proposed_transaction_type_label(duplicate) == ""


# ---------------------------------------------------------------------------
# Manual linking
# ---------------------------------------------------------------------------


def test_a_manual_link_records_the_users_own_judgement(owner: Any, data_key: bytes) -> None:
    rows = seed(owner, data_key)

    match = link_observations(
        user=owner,
        left_observation_id=rows[0].pk,
        right_observation_id=rows[1].pk,
        match_type=ReconciliationMatch.MatchType.REFUND_MATCH,
        data_key=data_key,
    )

    assert match.match_score == MANUAL_LINK_SCORE
    assert match.status == ReconciliationMatch.Status.PROPOSED
    assert decrypt_match_features(match, data_key=data_key) == ("manual_link",)


def test_a_manual_link_reopens_a_pairing_the_user_dismissed(owner: Any, data_key: bytes) -> None:
    """Detection must not resurrect a rejection; the person who made it may."""

    rows = seed(owner, data_key)
    match = record_match(
        user=owner,
        left_observation_id=rows[0].pk,
        right_observation_id=rows[1].pk,
        match_type=ReconciliationMatch.MatchType.REFUND_MATCH,
        score=70,
        data_key=data_key,
    )
    reject_match(match.pk, user=owner)

    reopened = link_observations(
        user=owner,
        left_observation_id=rows[0].pk,
        right_observation_id=rows[1].pk,
        match_type=ReconciliationMatch.MatchType.REFUND_MATCH,
        data_key=data_key,
    )

    assert reopened.pk == match.pk
    assert reopened.status == ReconciliationMatch.Status.PROPOSED
    assert reopened.reviewed_at is None
    assert reopened.match_score == MANUAL_LINK_SCORE


def test_detection_still_does_not_revive_a_rejected_pairing(owner: Any, data_key: bytes) -> None:
    rows = seed(owner, data_key)
    match = record_match(
        user=owner,
        left_observation_id=rows[0].pk,
        right_observation_id=rows[1].pk,
        match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
        score=95,
        data_key=data_key,
    )
    reject_match(match.pk, user=owner)

    for _ in range(3):
        record_match(
            user=owner,
            left_observation_id=rows[0].pk,
            right_observation_id=rows[1].pk,
            match_type=ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
            score=95,
            data_key=data_key,
        )

    match.refresh_from_db()
    assert match.status == ReconciliationMatch.Status.REJECTED
    assert ReconciliationMatch.objects.filter(user=owner).count() == 1


def test_an_unknown_relationship_cannot_be_linked(owner: Any, data_key: bytes) -> None:
    rows = seed(owner, data_key)

    with pytest.raises(ReconciliationError):
        link_observations(
            user=owner,
            left_observation_id=rows[0].pk,
            right_observation_id=rows[1].pk,
            match_type="not_a_relationship",
            data_key=data_key,
        )


def test_the_link_page_posts_a_candidate_and_offers_it_for_confirmation(
    owner: Any, data_key: bytes, client_for: Client
) -> None:
    rows = seed(owner, data_key)

    response = client_for.post(
        reverse("match-link"),
        data={
            "left_observation": str(rows[0].pk),
            "right_observation": str(rows[1].pk),
            "match_type": ReconciliationMatch.MatchType.INTERNAL_TRANSFER,
        },
    )

    match = ReconciliationMatch.objects.get(user=owner)
    assert response.status_code == 302
    assert response.headers["Location"] == reverse("match-detail", kwargs={"pk": match.pk})
    assert match.status == ReconciliationMatch.Status.PROPOSED
    # Linking records the relationship; it does not apply it.
    for row in rows:
        row.refresh_from_db()
        assert row.review_status == ImportedObservation.ReviewStatus.UNREVIEWED


def test_a_row_cannot_be_linked_to_itself(owner: Any, data_key: bytes, client_for: Client) -> None:
    rows = seed(owner, data_key)

    response = client_for.post(
        reverse("match-link"),
        data={
            "left_observation": str(rows[0].pk),
            "right_observation": str(rows[0].pk),
            "match_type": ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
        },
    )

    assert response.status_code == 400
    assert not ReconciliationMatch.objects.filter(user=owner).exists()


def test_another_users_row_cannot_be_linked(
    owner: Any, master_key: bytes, data_key: bytes, client_for: Client
) -> None:
    mine = seed(owner, data_key)
    stranger = make_user(email="queue-stranger@example.com")
    provision_user_data_key(user=stranger, actor=stranger, master_key=master_key)
    theirs = seed(
        stranger,
        get_user_data_key(user=stranger, actor=stranger, master_key=master_key),
    )

    response = client_for.post(
        reverse("match-link"),
        data={
            "left_observation": str(mine[0].pk),
            "right_observation": str(theirs[0].pk),
            "match_type": ReconciliationMatch.MatchType.DUPLICATE_OBSERVATION,
        },
    )

    assert response.status_code == 400
    assert not ReconciliationMatch.objects.exists()


def test_the_link_page_requires_a_login() -> None:
    response = Client().get(reverse("match-link"))

    assert response.status_code == 302
    assert reverse("login") in response.headers["Location"]
