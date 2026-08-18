"""A reviewer correcting what a screenshot is, and the parser obeying.

The behaviour under test is the whole point of the feature: detection guessed
wrong, a person said so, and the *next* pass must use their answer rather than
guessing again. So these tests go through the real parser registry rather than a
stub — a test that asserts a field was written proves nothing about whether the
parser ever reads it.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.core.errors import ForbiddenError
from apps.core.key_management import provision_user_data_key
from apps.ocr.pipeline import parse_ocr_runs
from apps.parsing.contracts import DocumentMetadata
from apps.parsing.registry import ParserSelectionError
from apps.processing.models import SourceDocument
from apps.processing.overrides import (
    OverrideError,
    institution_choices,
    institution_names,
    set_document_overrides,
)
from tests.factories import make_document, make_ocr_run, make_user

pytestmark = pytest.mark.django_db

PASSWORD = "override-password"

#: A Toss Bank transaction list. The markers are what detection would key on, so
#: a document overridden to a *different* institution proves the override wins
#: over evidence rather than merely filling a gap.
TOSS_TOKENS = (
    ("토스뱅크", 0, 10, 60, 24),
    ("거래내역", 70, 10, 130, 24),
    ("08.15", 0, 50, 60, 64),
    ("스타벅스", 70, 50, 140, 64),
    ("출금", 150, 50, 190, 64),
    ("4,200원", 200, 50, 280, 64),
)


@pytest.fixture
def master_key(tmp_path: Path, settings: Any) -> bytes:
    key = os.urandom(32)
    path = tmp_path / "master.key"
    path.write_text(base64.urlsafe_b64encode(key).decode(), encoding="ascii")
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)
    return key


@pytest.fixture
def owner(master_key: bytes) -> Any:
    user = make_user(email="override-owner@example.com", password=PASSWORD)
    provision_user_data_key(user=user, actor=user, master_key=master_key)
    return user


def toss_document(user: Any, **overrides: Any) -> SourceDocument:
    values: dict[str, Any] = {
        "source_type": SourceDocument.SourceType.BANK_TRANSACTION_LIST,
        "file_sha256": os.urandom(32).hex(),
    }
    values.update(overrides)
    return make_document(user, **values)


def persisted_run(user: Any, document: SourceDocument, data_key: bytes) -> Any:
    """One OCR run holding the Toss tokens, written the way the pipeline writes."""

    from apps.ocr.contracts import BoundingBox
    from apps.ocr.contracts import OcrToken as EngineToken
    from apps.ocr.services import persist_tokens

    run = make_ocr_run(user, document, engine="paddleocr")
    persist_tokens(
        run=run,
        tokens=tuple(
            EngineToken(text, 0.95, BoundingBox(left, top, right, bottom))
            for text, left, top, right, bottom in TOSS_TOKENS
        ),
        data_key=data_key,
        key_version=1,
    )
    return run


def selected_parser(document: SourceDocument, run: Any, data_key: bytes) -> str:
    return parse_ocr_runs(document, [run], data_key=data_key).metadata.name


# ----------------------------------------------------------------------
# Parser selection
# ----------------------------------------------------------------------


def test_an_override_selects_the_named_parser_on_the_next_pass(owner: Any) -> None:
    data_key = os.urandom(32)
    document = toss_document(owner)
    run = persisted_run(owner, document, data_key)

    assert selected_parser(document, run, data_key) == "toss_bank"

    set_document_overrides(document.pk, user=owner, institution="kakao_bank")
    document.refresh_from_db()

    # The screenshot still says 토스뱅크 on every line. The reviewer said
    # otherwise, and the reviewer wins.
    assert selected_parser(document, run, data_key) == "kakao_bank"


def test_clearing_an_override_restores_automatic_detection(owner: Any) -> None:
    data_key = os.urandom(32)
    document = toss_document(owner)
    run = persisted_run(owner, document, data_key)

    set_document_overrides(document.pk, user=owner, institution="kakao_bank")
    document.refresh_from_db()
    assert selected_parser(document, run, data_key) == "kakao_bank"

    set_document_overrides(document.pk, user=owner)
    document.refresh_from_db()
    assert document.institution_override == ""
    assert selected_parser(document, run, data_key) == "toss_bank"


def test_a_source_type_override_reaches_the_parser(owner: Any) -> None:
    document = toss_document(owner)
    assert document.effective_source_type == "bank_transaction_list"

    set_document_overrides(
        document.pk, user=owner, source_type=SourceDocument.SourceType.CREDIT_CARD_STATEMENT
    )
    document.refresh_from_db()

    # The detected guess survives, which is what keeps detection measurable.
    assert document.source_type == "bank_transaction_list"
    assert document.effective_source_type == "credit_card_statement"


def test_an_override_applies_to_its_own_document_only(owner: Any) -> None:
    data_key = os.urandom(32)
    overridden = toss_document(owner)
    untouched = toss_document(owner)
    overridden_run = persisted_run(owner, overridden, data_key)
    untouched_run = persisted_run(owner, untouched, data_key)

    set_document_overrides(overridden.pk, user=owner, institution="kakao_bank")
    overridden.refresh_from_db()
    untouched.refresh_from_db()

    assert selected_parser(overridden, overridden_run, data_key) == "kakao_bank"
    assert selected_parser(untouched, untouched_run, data_key) == "toss_bank"
    assert untouched.institution_override == ""


# ----------------------------------------------------------------------
# Ownership and validation
# ----------------------------------------------------------------------


def test_an_override_cannot_reach_another_users_document(owner: Any) -> None:
    intruder = make_user(email="override-intruder@example.com", password=PASSWORD)
    document = toss_document(owner)

    with pytest.raises(ForbiddenError):
        set_document_overrides(document.pk, user=intruder, institution="kakao_bank")

    document.refresh_from_db()
    assert document.institution_override == ""


def test_an_unregistered_parser_is_refused_rather_than_stored(owner: Any) -> None:
    document = toss_document(owner)

    with pytest.raises(OverrideError):
        set_document_overrides(document.pk, user=owner, institution="banco_imaginario")
    with pytest.raises(OverrideError):
        set_document_overrides(document.pk, user=owner, source_type="receipt_photo")

    document.refresh_from_db()
    assert not document.has_overrides


def test_every_offered_institution_is_a_parser_the_registry_can_run() -> None:
    """The choice list and the registry cannot drift apart."""

    from apps.ocr.pipeline import build_parser_registry

    registry = build_parser_registry()
    for name, display_name in institution_choices():
        assert display_name
        parser, _ = registry.select(
            DocumentMetadata("unknown", None, None, manual_source_override=name), ()
        )
        assert parser.metadata.name == name

    with pytest.raises(ParserSelectionError):
        registry.select(
            DocumentMetadata("unknown", None, None, manual_source_override="banco_imaginario"), ()
        )


# ----------------------------------------------------------------------
# Audit
# ----------------------------------------------------------------------


def test_setting_and_clearing_an_override_are_both_audited(owner: Any) -> None:
    document = toss_document(owner)

    set_document_overrides(document.pk, user=owner, institution="kakao_bank")
    event = owner.audit_events.filter(event_type="document_override_set").get()
    assert event.object_id == document.pk
    assert event.metadata["institution_override"] == "kakao_bank"
    assert event.metadata["detected_source_type"] == "bank_transaction_list"

    set_document_overrides(document.pk, user=owner)
    cleared = owner.audit_events.filter(event_type="document_override_cleared").get()
    assert cleared.metadata["previous_institution_override"] == "kakao_bank"
    assert cleared.metadata["institution_override"] == ""


def test_setting_the_same_override_twice_records_one_event(owner: Any) -> None:
    """Re-submitting an unchanged form is not an event worth keeping."""

    document = toss_document(owner)
    set_document_overrides(document.pk, user=owner, institution="kakao_bank")
    change = set_document_overrides(document.pk, user=owner, institution="kakao_bank")

    assert change.changed is False
    assert owner.audit_events.filter(event_type="document_override_set").count() == 1


# ----------------------------------------------------------------------
# The reviewer's path to it
# ----------------------------------------------------------------------


def test_a_reviewer_sets_and_clears_the_override_through_the_review_page(owner: Any) -> None:
    document = toss_document(owner)
    client = Client()
    client.force_login(owner)
    url = reverse("document-override", kwargs={"pk": document.pk})

    response = client.post(url, {"source_type": "credit_card_statement", "institution": "kb_bank"})
    assert response.status_code == 302
    document.refresh_from_db()
    assert document.source_type_override == "credit_card_statement"
    assert document.institution_override == "kb_bank"

    # An empty submission is how a reviewer goes back to automatic detection.
    client.post(url, {"source_type": "", "institution": ""})
    document.refresh_from_db()
    assert not document.has_overrides


def test_the_review_page_refuses_an_override_for_another_users_document(owner: Any) -> None:
    document = toss_document(owner)
    intruder = make_user(email="override-web-intruder@example.com", password=PASSWORD)
    client = Client()
    client.force_login(intruder)

    response = client.post(
        reverse("document-override", kwargs={"pk": document.pk}),
        {"source_type": "", "institution": "kb_bank"},
    )

    assert response.status_code == 404
    document.refresh_from_db()
    assert document.institution_override == ""


def test_an_unknown_institution_posted_directly_is_rejected(owner: Any) -> None:
    document = toss_document(owner)
    client = Client()
    client.force_login(owner)

    response = client.post(
        reverse("document-override", kwargs={"pk": document.pk}),
        {"source_type": "", "institution": "banco_imaginario"},
    )

    assert response.status_code == 302
    document.refresh_from_db()
    assert document.institution_override == ""
    assert "banco_imaginario" not in institution_names()
