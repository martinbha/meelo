from __future__ import annotations

from typing import Any

import pytest
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse
from django.test import Client, override_settings
from django.urls import path

from apps.core.errors import ConflictError, InvalidRequestError, ResourceNotFoundError
from config.urls import urlpatterns as application_urls

PRIVATE_TEXT = "Synthetic private financial detail"


def failing_view(request: HttpRequest, kind: str) -> HttpResponse:
    failures = {
        "invalid": InvalidRequestError(PRIVATE_TEXT, details={"free_text": PRIVATE_TEXT}),
        "conflict": ConflictError(PRIVATE_TEXT),
        "missing": ResourceNotFoundError(PRIVATE_TEXT),
        "http404": Http404(PRIVATE_TEXT),
        "forbidden": PermissionDenied(PRIVATE_TEXT),
        "unexpected": RuntimeError(PRIVATE_TEXT),
    }
    raise failures[kind]


urlpatterns = [path("error-probe/<str:kind>/", failing_view), *application_urls]


@override_settings(ROOT_URLCONF=__name__, DEBUG=False)
@pytest.mark.parametrize("mode", ["html", "json", "htmx"])
@pytest.mark.parametrize(
    ("kind", "status", "code"),
    [
        ("invalid", 400, "INVALID_REQUEST"),
        ("conflict", 409, "CONFLICT"),
        ("missing", 404, "RESOURCE_NOT_FOUND"),
        ("http404", 404, "RESOURCE_NOT_FOUND"),
        ("forbidden", 403, "FORBIDDEN"),
        ("unexpected", 500, "INTERNAL_SERVER_ERROR"),
    ],
)
def test_view_errors_cross_the_real_request_boundary_safely(
    client: Client,
    caplog: pytest.LogCaptureFixture,
    mode: str,
    kind: str,
    status: int,
    code: str,
) -> None:
    headers: dict[str, Any] = {"HTTP_X_REQUEST_ID": "error-boundary-test"}
    if mode == "json":
        headers["HTTP_ACCEPT"] = "application/json"
    elif mode == "htmx":
        headers["HTTP_HX_REQUEST"] = "true"

    response = client.post(f"/error-probe/{kind}/", {"notes": PRIVATE_TEXT}, **headers)

    assert response.status_code == status
    assert response["X-Error-Code"] == code
    assert response["X-Request-ID"] == "error-boundary-test"
    content = response.content.decode()
    assert PRIVATE_TEXT not in content
    assert PRIVATE_TEXT not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    if mode == "json":
        error = response.json()["error"]
        assert error["code"] == code
        assert error["request_id"] == "error-boundary-test"
        assert error["message"]
        assert error["recovery_hint"]
        assert error["details"] == {}
    else:
        assert code in content
        assert "error-boundary-test" in content
        assert ("<!doctype html>" in content) is (mode == "html")


@override_settings(ROOT_URLCONF=__name__, DEBUG=True)
def test_development_keeps_unexpected_exceptions_visible(client: Client) -> None:
    with pytest.raises(RuntimeError, match=PRIVATE_TEXT):
        client.get("/error-probe/unexpected/")


@pytest.mark.django_db
def test_incompatible_manual_card_returns_validation_error_without_creating_history(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.instruments.models import PaymentInstrument
    from apps.transactions.models import CanonicalTransaction
    from tests.factories import make_account, make_user

    owner = make_user()
    account = make_account(owner)
    other_account = make_account(owner, name_blind_index="other-account")
    card = PaymentInstrument.objects.create(
        user=owner,
        financial_account=other_account,
        instrument_type=PaymentInstrument.InstrumentType.DEBIT_CARD,
        name_encrypted="Synthetic card",
        name_blind_index="synthetic-card",
    )
    monkeypatch.setattr("apps.transactions.views._data_key", lambda request: b"d" * 32)
    monkeypatch.setattr("apps.transactions.views.request_search_key", lambda request: b"s" * 32)
    client.force_login(owner)

    response = client.post(
        "/transactions/new/",
        {
            "occurred_at": "2026-08-07",
            "amount": "100",
            "currency": "KRW",
            "transaction_type": "purchase",
            "financial_account": str(account.pk),
            "payment_instrument": str(card.pk),
        },
        HTTP_ACCEPT="application/json",
    )

    assert response.status_code == 400
    assert response["X-Error-Code"] == "INVALID_REQUEST"
    assert not CanonicalTransaction.objects.filter(user=owner).exists()


def test_duplicate_upload_explicitly_exposes_only_its_safe_document_identifier() -> None:
    from uuid import uuid4

    from apps.processing.models import SourceDocument
    from apps.processing.upload_services import DuplicateUploadError

    document = SourceDocument(pk=uuid4())
    error = DuplicateUploadError(document)
    error.details["notes"] = PRIVATE_TEXT
    assert error.public_details == {"document_id": str(document.pk)}


@pytest.mark.django_db
def test_bound_form_errors_are_not_replaced_by_exception_handling(client: Client) -> None:
    response = client.post("/login/", {"username": "", "password": ""})
    form: Any = response.context["form"]
    assert response.status_code == 200
    assert form.errors
    assert "X-Error-Code" not in response
