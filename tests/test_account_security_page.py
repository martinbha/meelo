"""One page for the state of an account, and nothing about its money (#170).

The privacy property here is unusual and worth stating. This is the page
somebody opens *because* they are uneasy — often at a desk, often not alone —
so a financial figure on it is a figure shown to whoever is standing there.
Nothing on this page comes from a financial model, and a test asserts that
rather than trusting the template to stay that way.

Audit entries are rendered as codes. Their metadata carries identifiers and
counts that were safe to store precisely because nothing renders them; putting
them on a page would make that assumption false everywhere at once.
"""

from __future__ import annotations

import base64
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.core.audit import record_audit_event
from apps.core.key_management import get_user_data_key, provision_user_data_key
from apps.transactions.models import CanonicalTransaction
from apps.transactions.services import create_manual_transaction
from apps.users.security import RECENT_EVENT_LIMIT, security_overview
from tests.factories import make_account, make_user

pytestmark = pytest.mark.django_db

PASSWORD = "security-page-password"
MERCHANT = "스타벅스 강남점"


@pytest.fixture
def master_key(tmp_path: Path, settings: Any) -> bytes:
    key = os.urandom(32)
    path = tmp_path / "master.key"
    path.write_text(base64.urlsafe_b64encode(key).decode(), encoding="ascii")
    path.chmod(0o600)
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)
    return key


@pytest.fixture
def owner(master_key: bytes) -> Any:
    user = make_user(email="security-owner@example.com", password=PASSWORD)
    provision_user_data_key(user=user, actor=user, master_key=master_key)
    return user


@pytest.fixture
def signed_in(owner: Any) -> Client:
    client = Client()
    client.force_login(owner)
    return client


def page(client: Client) -> Any:
    return client.get(reverse("account-security"))


# ----------------------------------------------------------------------
# Authentication and scope
# ----------------------------------------------------------------------


def test_the_page_requires_authentication() -> None:
    response = Client().get(reverse("account-security"))

    assert response.status_code == 302
    assert reverse("login") in response["Location"]


def test_the_page_shows_only_the_current_users_data(owner: Any, signed_in: Client) -> None:
    stranger = make_user(email="security-stranger@example.com", password=PASSWORD)
    TOTPDevice.objects.create(user=stranger, name="their phone", confirmed=True)
    record_audit_event(user=stranger, event_type="login_failure", metadata={})
    Client().force_login(stranger)

    overview = page(signed_in).context["overview"]

    assert overview.email == owner.email
    assert overview.two_factor_enabled is False
    assert overview.failed_logins_recently == 0
    assert all(session.is_current for session in overview.sessions)


def test_a_session_belonging_to_someone_else_is_not_listed(owner: Any, signed_in: Client) -> None:
    stranger = make_user(email="security-other@example.com", password=PASSWORD)
    other_client = Client()
    other_client.force_login(stranger)

    overview = page(signed_in).context["overview"]

    assert len(overview.sessions) == 1
    assert overview.sessions[0].is_current


# ----------------------------------------------------------------------
# What it shows
# ----------------------------------------------------------------------


def test_the_page_reports_password_age_from_the_audit_log(owner: Any, signed_in: Client) -> None:
    """Django does not record it; the audit log is the only place it exists."""

    record_audit_event(user=owner, event_type="password_changed", metadata={})

    overview = page(signed_in).context["overview"]

    assert overview.password_changed_at is not None
    assert overview.password_age_days == 0
    assert overview.password_is_old is False


def test_an_account_that_never_changed_its_password_falls_back_to_its_join_date(
    owner: Any, signed_in: Client
) -> None:
    overview = page(signed_in).context["overview"]

    assert overview.password_changed_at == owner.date_joined


def test_an_old_password_is_flagged_without_being_expired(owner: Any) -> None:
    owner.date_joined = timezone.now() - timedelta(days=400)
    owner.save(update_fields=["date_joined"])

    overview = security_overview(owner)

    assert overview.password_age_days is not None
    assert overview.password_age_days >= 400
    assert overview.password_is_old is True


def test_the_page_reports_two_factor_state_and_recovery_codes(
    owner: Any, signed_in: Client
) -> None:
    TOTPDevice.objects.create(user=owner, name="phone", confirmed=True)
    TOTPDevice.objects.create(user=owner, name="unconfirmed", confirmed=False)
    device = StaticDevice.objects.create(user=owner, name="recovery")
    for index in range(3):
        StaticToken.objects.create(device=device, token=f"code-{index}")

    overview = page(signed_in).context["overview"]

    assert overview.two_factor_enabled is True
    # An unconfirmed device is a half-finished enrolment, not a second factor.
    assert overview.confirmed_device_count == 1
    assert overview.recovery_codes_remaining == 3


def test_recent_failed_sign_ins_are_surfaced(owner: Any, signed_in: Client) -> None:
    for _ in range(2):
        record_audit_event(user=owner, event_type="login_failure", metadata={})

    overview = page(signed_in).context["overview"]

    assert overview.failed_logins_recently == 2


def test_the_event_list_is_bounded(owner: Any, signed_in: Client) -> None:
    for _ in range(RECENT_EVENT_LIMIT + 5):
        record_audit_event(user=owner, event_type="login_success", metadata={})

    overview = page(signed_in).context["overview"]

    assert len(overview.recent_events) == RECENT_EVENT_LIMIT


def test_workflow_events_are_not_shown_on_a_security_page(owner: Any, signed_in: Client) -> None:
    """A list of everything somebody did last week is a different page."""

    record_audit_event(user=owner, event_type="transaction_created", metadata={})
    record_audit_event(user=owner, event_type="login_success", metadata={})

    codes = {event.code for event in page(signed_in).context["overview"].recent_events}

    assert codes == {"login_success"}


# ----------------------------------------------------------------------
# What it must not show
# ----------------------------------------------------------------------


def test_no_financial_value_reaches_the_page(
    owner: Any, signed_in: Client, master_key: bytes
) -> None:
    data_key = get_user_data_key(user=owner, actor=owner, master_key=master_key)
    create_manual_transaction(
        user=owner,
        occurred_at=date(2026, 8, 15),
        amount_minor=42_900,
        currency="KRW",
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
        financial_account=make_account(owner),
        merchant=MERCHANT,
        counterparty="김대성",
        data_key=data_key,
    )

    body = page(signed_in).content.decode()

    for value in (MERCHANT, "김대성", "42900", "42,900"):
        assert value not in body


def test_no_key_material_reaches_the_page(owner: Any, signed_in: Client, master_key: bytes) -> None:
    device = TOTPDevice.objects.create(user=owner, name="phone", confirmed=True)
    static = StaticDevice.objects.create(user=owner, name="recovery")
    token = StaticToken.objects.create(device=static, token="a-recovery-code")
    data_key = get_user_data_key(user=owner, actor=owner, master_key=master_key)

    body = page(signed_in).content.decode()

    assert device.key not in body
    assert token.token not in body
    assert base64.urlsafe_b64encode(master_key).decode() not in body
    assert base64.urlsafe_b64encode(data_key).decode() not in body
    assert data_key.hex() not in body


def test_a_session_identifier_is_never_rendered_in_full(owner: Any, signed_in: Client) -> None:
    """The full key is a bearer credential: reading it off the screen signs you in."""

    session_key = signed_in.session.session_key
    assert session_key

    body = page(signed_in).content.decode()
    overview = page(signed_in).context["overview"]

    assert session_key not in body
    assert all(len(session.key_prefix) <= 8 for session in overview.sessions)


def test_audit_metadata_is_not_rendered(owner: Any, signed_in: Client) -> None:
    """Metadata was safe to store because nothing renders it. Keep it that way."""

    record_audit_event(
        user=owner,
        event_type="login_success",
        metadata={"method": "password", "secret_looking_value": "must-not-appear"},
    )

    body = page(signed_in).content.decode()

    assert "must-not-appear" not in body
    assert "login_success" in body


# ----------------------------------------------------------------------
# The links
# ----------------------------------------------------------------------


def test_the_page_links_to_the_actions_it_describes(owner: Any, signed_in: Client) -> None:
    body = page(signed_in).content.decode()

    assert reverse("password-change") in body
