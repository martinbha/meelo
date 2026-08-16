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
from apps.ocr.contracts import BoundingBox
from apps.parsing.contracts import (
    ParsedObservation,
    ParserMetadata,
    ParserSupport,
    TransactionDirection,
)
from apps.parsing.registry import ParserSelection
from apps.processing.models import SourceDocument
from apps.processing.storage import document_directory
from tests.factories import make_account, make_document, make_ocr_run, make_user

pytestmark = pytest.mark.django_db

PASSWORD = "review-view-password"
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)


@pytest.fixture
def master_key(tmp_path: Path, settings: Any) -> bytes:
    import base64

    key = os.urandom(32)
    path = tmp_path / "master.key"
    path.write_text(base64.urlsafe_b64encode(key).decode(), encoding="ascii")
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)
    settings.DOCUMENT_TMP_ROOT = str(tmp_path / "documents")
    return key


@pytest.fixture
def owner(master_key: bytes) -> Any:
    user = make_user(email="view-owner@example.com", password=PASSWORD)
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
        "source_region": BoundingBox(0, 10, 300, 40),
        "confidence_factors": {"token_confidence": 0.95, "amount_confidence": 0.95},
        "parser_name": "toss_bank",
        "parser_version": "1.0",
        "parser_support_score": 0.95,
    }
    values.update(overrides)
    return ParsedObservation(**values)


def seed(user: Any, master_key: bytes, *observations: ParsedObservation) -> Any:
    document = make_document(user, processing_status=SourceDocument.Status.READY_FOR_REVIEW)
    run = make_ocr_run(user, document)
    data_key = get_user_data_key(user=user, actor=user, master_key=master_key)
    rows = import_parser_selection(
        document=document,
        ocr_run=run,
        selection=ParserSelection(
            ParserMetadata("toss_bank", "1.0"),
            ParserSupport(0.95, "bank_transaction_list", ()),
            observations or (parsed(),),
        ),
        data_key=data_key,
        key_version=1,
    ).observations
    return document, rows


def store_image(document: SourceDocument) -> Path:
    directory = document_directory(document.pk)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "original.png"
    path.write_bytes(PNG)
    return path


def test_the_queue_lists_open_rows_for_its_owner(
    owner: Any, master_key: bytes, client_for: Client
) -> None:
    seed(owner, master_key, parsed(), parsed(merchant="두번째"))

    response = client_for.get(reverse("review-queue"))

    assert response.status_code == 200
    assert response.context["page"].total == 2


def test_the_queue_never_shows_another_users_rows(owner: Any, master_key: bytes) -> None:
    seed(owner, master_key, parsed())
    intruder = make_user(email="view-intruder@example.com", password=PASSWORD)
    provision_user_data_key(user=intruder, actor=intruder, master_key=master_key)
    client = Client()
    client.force_login(intruder)

    response = client.get(reverse("review-queue"))

    assert response.context["page"].total == 0


def test_the_review_page_shows_rows_and_their_regions(
    owner: Any, master_key: bytes, client_for: Client
) -> None:
    document, _ = seed(owner, master_key, parsed())
    store_image(document)

    response = client_for.get(reverse("observation-review", kwargs={"pk": document.pk}))

    assert response.status_code == 200
    rows = response.context["rows"]
    assert len(rows) == 1
    # Every extracted field can be located on the screenshot.
    assert rows[0]["region"] == {"left": 0, "top": 10, "right": 300, "bottom": 40}
    assert rows[0]["values"].merchant == "스타벅스"
    assert response.context["has_image"] is True


def test_flagged_rows_are_marked_for_the_reviewer(
    owner: Any, master_key: bytes, client_for: Client
) -> None:
    document, _ = seed(owner, master_key, parsed(ambiguous_fields=frozenset({"amount"})))

    response = client_for.get(reverse("observation-review", kwargs={"pk": document.pk}))
    body = response.content.decode()

    assert "ocr-region-flagged" in body or "review-row-flagged" in body
    assert "ambiguous_amount" in body
    # A disputed amount must demand explicit confirmation before acceptance.
    assert "confirm-required" in body


def test_the_original_image_is_served_only_to_its_owner(
    owner: Any, master_key: bytes, client_for: Client
) -> None:
    document, _ = seed(owner, master_key, parsed())
    store_image(document)

    allowed = client_for.get(reverse("document-image", kwargs={"pk": document.pk}))
    assert allowed.status_code == 200
    assert allowed["Content-Type"] == "image/png"
    assert allowed["Cache-Control"] == "private, no-store"

    intruder = make_user(email="image-intruder@example.com", password=PASSWORD)
    provision_user_data_key(user=intruder, actor=intruder, master_key=master_key)
    other = Client()
    other.force_login(intruder)
    assert other.get(reverse("document-image", kwargs={"pk": document.pk})).status_code == 404


def test_the_original_image_requires_a_session(owner: Any, master_key: bytes) -> None:
    document, _ = seed(owner, master_key, parsed())
    store_image(document)

    response = Client().get(reverse("document-image", kwargs={"pk": document.pk}))

    assert response.status_code == 302
    assert "/login/" in response["Location"]


def test_a_correction_posted_from_the_page_is_applied(
    owner: Any, master_key: bytes, client_for: Client
) -> None:
    document, rows = seed(owner, master_key, parsed())

    response = client_for.post(
        reverse("observation-action", kwargs={"pk": rows[0].pk, "action": "correct"}),
        data={
            "merchant": "이디야커피",
            "occurred_at": "2026-08-15",
            "amount_minor": "4200",
            "currency": "KRW",
            "direction": "debit",
            "transaction_type_guess": "unknown",
        },
        follow=True,
    )

    rows[0].refresh_from_db()
    assert response.status_code == 200
    assert rows[0].corrected_fields == ["merchant"]
    assert rows[0].review_status == ImportedObservation.ReviewStatus.CORRECTED


def test_accepting_from_the_page_records_a_transaction(
    owner: Any, master_key: bytes, client_for: Client
) -> None:
    document, rows = seed(owner, master_key, parsed())
    account = make_account(owner)

    client_for.post(
        reverse("observation-action", kwargs={"pk": rows[0].pk, "action": "accept"}),
        data={
            "financial_account": str(account.pk),
            "transaction_type": "purchase",
        },
        follow=True,
    )

    rows[0].refresh_from_db()
    assert rows[0].canonical_transaction_id is not None


def test_actions_on_another_users_row_are_not_found(owner: Any, master_key: bytes) -> None:
    _, rows = seed(owner, master_key, parsed())
    intruder = make_user(email="action-intruder@example.com", password=PASSWORD)
    provision_user_data_key(user=intruder, actor=intruder, master_key=master_key)
    client = Client()
    client.force_login(intruder)

    response = client.post(
        reverse("observation-action", kwargs={"pk": rows[0].pk, "action": "reject"})
    )

    assert response.status_code == 404
    rows[0].refresh_from_db()
    assert rows[0].review_status == ImportedObservation.ReviewStatus.UNREVIEWED


def test_an_unknown_action_is_not_found(owner: Any, master_key: bytes, client_for: Client) -> None:
    _, rows = seed(owner, master_key, parsed())

    response = client_for.post(
        reverse("observation-action", kwargs={"pk": rows[0].pk, "action": "obliterate"})
    )

    assert response.status_code == 404


def test_reprocessing_can_be_requested_from_the_review_page(
    owner: Any, master_key: bytes, client_for: Client
) -> None:
    document, _ = seed(owner, master_key, parsed())

    client_for.post(reverse("document-reprocess", kwargs={"pk": document.pk}), follow=True)

    document.refresh_from_db()
    assert document.processing_status == SourceDocument.Status.QUEUED
