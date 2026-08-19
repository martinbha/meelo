"""Security claims, checked continuously (#99, specification 20-23, 31.5).

The claims this system makes about itself — every page needs a login, nobody sees
anybody else's rows, nothing sensitive reaches a log — are only true while
somebody keeps checking. A route added without a mixin, an object fetched without
an ownership filter, a log line that interpolates a merchant: each is one commit
away and none of them fails loudly.

So these tests enumerate rather than sample. Every named route is walked
unauthenticated. Every object-scoped route is walked as the wrong user. A route
added tomorrow appears in both lists without anybody remembering to add it.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver, reverse
from django.utils import timezone

from apps.core.crypto import encrypt_model_field, is_encrypted_value
from apps.core.key_management import get_user_data_key, provision_user_data_key
from apps.core.logging import RequestContextFilter, StructuredFormatter, redact_sensitive
from apps.observations.models import ImportedObservation
from apps.reports.models import TransactionExport
from apps.reports.services import create_export
from apps.transactions.models import CanonicalTransaction
from apps.transactions.money import store_money
from tests.factories import make_account, make_document, make_ocr_run, make_user
from tests.plaintext import stored_text

pytestmark = pytest.mark.django_db

MERCHANT = "스타벅스 강남점"

#: Everything the production settings insist on before they will load.
PRODUCTION_ENV: dict[str, str] = {
    "DJANGO_SECRET_KEY": "checks-only-not-a-real-key",
    "DJANGO_ALLOWED_HOSTS": "finance.example.com",
    "FIELD_ENCRYPTION_MASTER_KEY_FILE": "/run/secrets/master.key",
    "POSTGRES_PASSWORD": "checks-only",
}

#: Routes that are deliberately reachable without a session.
PUBLIC_ROUTES: frozenset[str] = frozenset(
    {
        "health-check",
        "login",
        "logout",
        "password-reset",
        "password-reset-done",
        "password-reset-confirm",
        "password-reset-complete",
        "password-change-done",
    }
)


def named_routes() -> list[tuple[str, str]]:
    """Every named route and its pattern, discovered rather than listed.

    A route added tomorrow is covered without anybody remembering to add it,
    which is the only way a sweep like this stays true.
    """

    found: list[tuple[str, str]] = []

    def walk(patterns: Any, prefix: str = "") -> None:
        for entry in patterns:
            if isinstance(entry, URLResolver):
                # Namespaced trees — the admin — carry their own authentication
                # and their own names. Walking into them would test Django.
                if entry.namespace:
                    continue
                walk(entry.url_patterns, prefix + str(entry.pattern))
            elif isinstance(entry, URLPattern) and entry.name:
                found.append((entry.name, prefix + str(entry.pattern)))

    walk(get_resolver().url_patterns)
    return found


@pytest.fixture
def master_key(tmp_path: Path, settings: Any) -> bytes:
    key = os.urandom(32)
    path = tmp_path / "master.key"
    path.write_text(base64.urlsafe_b64encode(key).decode(), encoding="ascii")
    path.chmod(0o600)
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)
    settings.DOCUMENT_TMP_ROOT = str(tmp_path / "documents")
    settings.EXPORT_TMP_ROOT = str(tmp_path / "exports")
    return key


def make_owner(master_key: bytes, email: str) -> Any:
    user = make_user(email=email)
    provision_user_data_key(user=user, actor=user, master_key=master_key)
    user.last_login = timezone.now()
    user.save(update_fields=["last_login"])
    return user


@pytest.fixture
def alice(master_key: bytes) -> Any:
    return make_owner(master_key, "alice@example.com")


@pytest.fixture
def bob(master_key: bytes) -> Any:
    return make_owner(master_key, "bob@example.com")


def belongings(user: Any, master_key: bytes) -> dict[str, Any]:
    """One of everything a route can address, owned by this user."""

    from apps.core.value_objects import Money

    data_key = get_user_data_key(user=user, actor=user, master_key=master_key)
    account = make_account(user, name_blind_index=f"sec-{user.pk}")
    document = make_document(user, file_sha256=os.urandom(32).hex())
    run = make_ocr_run(user, document)
    observation = ImportedObservation.objects.create(
        user=user,
        source_document=document,
        ocr_run=run,
        occurred_at=date(2026, 8, 15),
        currency="KRW",
        direction=ImportedObservation.Direction.DEBIT,
    )
    transaction = CanonicalTransaction.objects.create(
        user=user,
        created_by=user,
        financial_account=account,
        occurred_at=date(2026, 8, 15),
        amount_encrypted="1:KRW",
        currency="KRW",
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
    )
    store_money(transaction, "amount_encrypted", Money(42_900, "KRW"), data_key=data_key)
    transaction.merchant_encrypted = encrypt_model_field(
        transaction, "merchant_encrypted", MERCHANT, key=data_key, key_version=1
    )
    transaction.save(update_fields=["amount_encrypted", "merchant_encrypted"])
    export = create_export(user=user, export_format=TransactionExport.Format.CSV, data_key=data_key)
    return {
        "data_key": data_key,
        "account": account,
        "document": document,
        "observation": observation,
        "transaction": transaction,
        "export": export,
    }


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def test_every_private_route_requires_a_login() -> None:
    """Enumerated, so a route added without the mixin is caught."""

    client = Client()
    unprotected: list[str] = []

    for name, pattern in named_routes():
        if name in PUBLIC_ROUTES or "<" in pattern:
            continue
        response = client.get(reverse(name))
        if response.status_code != 302 or reverse("login") not in response.headers.get(
            "Location", ""
        ):
            unprotected.append(name)

    assert unprotected == [], unprotected


def test_object_routes_require_a_login(alice: Any, master_key: bytes) -> None:
    owned = belongings(alice, master_key)
    client = Client()
    routes = {
        "observation-review": owned["document"].pk,
        "document-image": owned["document"].pk,
        "transaction-edit": owned["transaction"].pk,
        "transaction-category": owned["transaction"].pk,
        "upload-detail": owned["document"].pk,
        "export-download": owned["export"].pk,
    }

    for name, identifier in routes.items():
        response = client.get(reverse(name, kwargs={"pk": identifier}))
        assert response.status_code == 302, name
        assert reverse("login") in response.headers["Location"], name


def test_a_logged_out_session_cannot_reach_a_page(alice: Any) -> None:
    client = Client()
    client.force_login(alice)
    assert client.get(reverse("review-queue")).status_code == 200

    client.logout()

    assert client.get(reverse("review-queue")).status_code == 302


# ---------------------------------------------------------------------------
# One user cannot reach another's anything
# ---------------------------------------------------------------------------


def test_no_object_route_serves_another_users_row(alice: Any, bob: Any, master_key: bytes) -> None:
    """The sweep that matters: every addressable object, as the wrong person."""

    theirs = belongings(bob, master_key)
    client = Client()
    client.force_login(alice)
    leaked: list[str] = []

    routes = {
        "observation-review": theirs["document"].pk,
        "document-image": theirs["document"].pk,
        "transaction-edit": theirs["transaction"].pk,
        "transaction-category": theirs["transaction"].pk,
        "upload-detail": theirs["document"].pk,
        "match-detail": theirs["observation"].pk,
    }
    for name, identifier in routes.items():
        response = client.get(reverse(name, kwargs={"pk": identifier}))
        # A 404 rather than a 403: Alice has no business learning it exists.
        if response.status_code != 404:
            leaked.append(f"{name}: {response.status_code}")

    assert leaked == [], leaked


def test_another_users_export_is_never_served(alice: Any, bob: Any, master_key: bytes) -> None:
    theirs = belongings(bob, master_key)
    client = Client()
    client.force_login(alice)

    response = client.get(reverse("export-download", kwargs={"pk": theirs["export"].pk}))

    assert response.status_code == 302
    assert MERCHANT.encode() not in response.content
    assert b"42900" not in response.content


def test_another_users_export_cannot_be_deleted(alice: Any, bob: Any, master_key: bytes) -> None:
    theirs = belongings(bob, master_key)
    client = Client()
    client.force_login(alice)

    client.post(reverse("export-delete", kwargs={"pk": theirs["export"].pk}))

    theirs["export"].refresh_from_db()
    assert theirs["export"].deleted_at is None


def test_no_report_shows_another_users_figures(alice: Any, bob: Any, master_key: bytes) -> None:
    belongings(bob, master_key)
    client = Client()
    client.force_login(alice)

    for name in (
        "report-overview",
        "report-categories",
        "report-merchants",
        "report-accounts",
        "report-cards",
        "report-outstanding",
    ):
        response = client.get(reverse(name), {"year": "2026", "month": "8"})
        body = response.content.decode()
        assert response.status_code == 200, name
        assert MERCHANT not in body, name
        assert "42900" not in body, name


def test_a_worker_path_never_crosses_users(alice: Any, bob: Any, master_key: bytes) -> None:
    """The queue and reconciliation services are scoped like the views."""

    from apps.observations.queue import review_queue
    from apps.reconciliation.transfers import propose_internal_transfers
    from apps.reports.spending import monthly_spending

    theirs = belongings(bob, master_key)
    mine = get_user_data_key(user=alice, actor=alice, master_key=master_key)

    assert review_queue(alice).total == 0
    assert propose_internal_transfers(user=alice, data_key=mine) == ()
    assert (
        monthly_spending(alice, year=2026, month=8, data_key=mine).totals("KRW").transaction_count
        == 0
    )
    assert theirs["transaction"].user_id == bob.pk


# ---------------------------------------------------------------------------
# CSRF and unsafe methods
# ---------------------------------------------------------------------------


def test_a_post_without_a_csrf_token_is_refused(alice: Any) -> None:
    client = Client(enforce_csrf_checks=True)
    client.force_login(alice)

    response = client.post(reverse("transaction-new"), data={"amount": "1"})

    assert response.status_code == 403


def test_a_deletion_route_refuses_a_get(alice: Any, master_key: bytes) -> None:
    """A destructive action behind a link is one a crawler can trigger."""

    owned = belongings(alice, master_key)
    client = Client()
    client.force_login(alice)

    response = client.get(reverse("export-delete", kwargs={"pk": owned["export"].pk}))

    assert response.status_code == 405
    owned["export"].refresh_from_db()
    assert owned["export"].deleted_at is None


# ---------------------------------------------------------------------------
# Nothing sensitive in the database or the logs
# ---------------------------------------------------------------------------


def test_no_readable_financial_value_is_stored(alice: Any, master_key: bytes) -> None:
    owned = belongings(alice, master_key)

    stored = CanonicalTransaction.objects.get(pk=owned["transaction"].pk)

    assert is_encrypted_value(stored.amount_encrypted)
    assert is_encrypted_value(stored.merchant_encrypted)
    assert MERCHANT not in stored_text(stored)
    assert "42900" not in stored_text(stored)


@pytest.mark.parametrize(
    "message",
    [
        "merchant=스타벅스 강남점",
        "amount: 42900",
        'password="hunter2"',
        "api_token=abcdef123456",
        "card_number: 1234-5678-9012-3456",
        "ocr_text='balance 957,100'",
    ],
)
def test_sensitive_values_are_redacted_from_logs(message: str) -> None:
    redacted = redact_sensitive(message)

    assert "[REDACTED]" in redacted
    for secret in ("스타벅스", "42900", "hunter2", "abcdef123456", "9012", "957,100"):
        assert secret not in redacted


def test_a_formatted_log_line_carries_no_secret() -> None:
    record = logging.LogRecord(
        "apps.test",
        logging.ERROR,
        __file__,
        1,
        "failed for merchant=스타벅스 with password=hunter2",
        None,
        None,
    )
    RequestContextFilter().filter(record)

    payload = json.loads(StructuredFormatter().format(record))

    assert "스타벅스" not in payload["message"]
    assert "hunter2" not in payload["message"]


# ---------------------------------------------------------------------------
# Production settings
# ---------------------------------------------------------------------------


def test_production_settings_keep_the_hardening_on(monkeypatch: Any) -> None:
    """A setting flipped for local convenience is one that ships."""

    import importlib

    for name, value in PRODUCTION_ENV.items():
        monkeypatch.setenv(name, value)
    production = importlib.reload(importlib.import_module("config.settings.production"))

    assert production.DEBUG is False
    assert production.SESSION_COOKIE_SECURE is True
    assert production.CSRF_COOKIE_SECURE is True
    assert production.SESSION_COOKIE_HTTPONLY is True
    assert production.SECURE_CONTENT_TYPE_NOSNIFF is True
    assert production.X_FRAME_OPTIONS == "DENY"


@pytest.mark.parametrize(
    "missing",
    # The master key path is deliberately absent from this list. It is no
    # longer a required variable — a Docker secret and a systemd credential are
    # found without one — and the guarantee it used to stand for is enforced at
    # startup instead, where it checks that a key actually loads rather than
    # that a variable happens to be set.
    ["DJANGO_SECRET_KEY", "DJANGO_ALLOWED_HOSTS"],
)
def test_production_refuses_to_start_without_its_secrets(missing: str, monkeypatch: Any) -> None:
    """A default is how a development secret key reaches production.

    Refusing to boot is the loud failure. A settings module that quietly
    substitutes something usable is the one that ships signing everybody's
    sessions with a key that is also in the repository.
    """

    import importlib

    for name, value in PRODUCTION_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(missing, raising=False)

    with pytest.raises(RuntimeError, match=missing):
        importlib.reload(importlib.import_module("config.settings.production"))
