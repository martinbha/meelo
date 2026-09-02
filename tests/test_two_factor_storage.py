"""The two-factor foundation, installed before anything depends on it (#169, specification 20.2).

Deliberately the storage and nothing else. Enrolment is #171 and enforcement is
#173, and the order matters: a login wall that arrives before the enrolment flow
does is a locked account, not a hardened one.

So what is tested here is that the tables exist, that a device belongs to
exactly one person, that existing logins are untouched — and that the device's
shared secret cannot be read back out through the two places it would otherwise
appear.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib import admin
from django.test import Client
from django.urls import reverse
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

from tests.factories import make_user

pytestmark = pytest.mark.django_db

PASSWORD = "two-factor-password"


@pytest.fixture
def owner() -> Any:
    return make_user(email="otp-owner@example.com", password=PASSWORD)


# ----------------------------------------------------------------------
# Installed, and not yet in the way
# ----------------------------------------------------------------------


def test_the_device_tables_exist(owner: Any) -> None:
    device = TOTPDevice.objects.create(user=owner, name="phone", confirmed=False)
    static = StaticDevice.objects.create(user=owner, name="recovery")

    assert TOTPDevice.objects.get(pk=device.pk).user_id == owner.pk
    assert StaticDevice.objects.get(pk=static.pk).user_id == owner.pk


def test_the_middleware_annotates_a_request_without_blocking_it(owner: Any) -> None:
    """`is_verified` is present and false; nothing acts on it yet."""

    client = Client()
    client.force_login(owner)

    response = client.get(reverse("transaction-list"))

    assert response.status_code == 200
    assert hasattr(response.wsgi_request.user, "is_verified")
    assert response.wsgi_request.user.is_verified() is False


def test_an_existing_login_still_works_with_no_device(owner: Any) -> None:
    """Two-factor stays optional until the enrolment flow exists."""

    client = Client()

    response = client.post(reverse("login"), {"username": owner.email, "password": PASSWORD})

    assert response.status_code == 302
    assert "_auth_user_id" in client.session
    assert client.get(reverse("transaction-list")).status_code == 200
    assert not TOTPDevice.objects.filter(user=owner).exists()


def test_a_confirmed_device_requires_verification_after_password_login(owner: Any) -> None:

    TOTPDevice.objects.create(user=owner, name="phone", confirmed=True)
    client = Client()

    response = client.post(reverse("login"), {"username": owner.email, "password": PASSWORD})

    assert response.status_code == 302
    assert response["Location"] == reverse("two-factor-verify")
    assert client.get(reverse("transaction-list"))["Location"] == reverse("two-factor-verify")


# ----------------------------------------------------------------------
# One device, one person
# ----------------------------------------------------------------------


def test_a_device_belongs_to_exactly_one_user(owner: Any) -> None:
    stranger = make_user(email="otp-stranger@example.com", password=PASSWORD)
    mine = TOTPDevice.objects.create(user=owner, name="phone", confirmed=True)
    theirs = TOTPDevice.objects.create(user=stranger, name="phone", confirmed=True)

    assert list(TOTPDevice.objects.filter(user=owner)) == [mine]
    assert list(TOTPDevice.objects.filter(user=stranger)) == [theirs]
    # And two people's devices do not share a seed, even created together.
    assert mine.key != theirs.key


def test_deleting_a_user_takes_their_devices_with_them(owner: Any) -> None:
    TOTPDevice.objects.create(user=owner, name="phone")
    StaticDevice.objects.create(user=owner, name="recovery")

    owner.delete()

    assert not TOTPDevice.objects.exists()
    assert not StaticDevice.objects.exists()


# ----------------------------------------------------------------------
# The secret stays where it is
# ----------------------------------------------------------------------


def test_the_administration_form_does_not_render_the_seed(owner: Any) -> None:
    """A staff account that can read the seed can generate codes forever.

    django-otp's own admin exposes it as an editable field, which makes the
    second factor worth nothing while looking entirely present.
    """

    from django.test import RequestFactory

    device_admin = admin.site._registry[TOTPDevice]
    request = RequestFactory().get("/admin/")

    assert "key" not in device_admin.get_fields(request)
    assert "key" not in (device_admin.fields or ())


def test_the_recovery_token_table_is_not_administrable(owner: Any) -> None:
    assert not admin.site.is_registered(StaticToken)


def test_the_device_admin_is_reachable_only_by_staff(owner: Any) -> None:
    TOTPDevice.objects.create(user=owner, name="phone")
    url = reverse("admin:otp_totp_totpdevice_changelist")
    client = Client()
    client.force_login(owner)

    assert owner.is_staff is False
    assert client.get(url).status_code in {302, 403}


def test_a_staff_user_sees_the_device_without_its_seed(owner: Any) -> None:
    staff = make_user(email="otp-staff@example.com", password=PASSWORD)
    staff.is_staff = True
    staff.is_superuser = True
    staff.save(update_fields=["is_staff", "is_superuser"])
    device = TOTPDevice.objects.create(user=owner, name="phone", confirmed=True)
    client = Client()
    client.force_login(staff)

    response = client.get(reverse("admin:otp_totp_totpdevice_change", args=[device.pk]))

    assert response.status_code == 200
    body = response.content.decode()
    assert "phone" in body
    assert device.key not in body


def test_a_seed_is_redacted_from_logs() -> None:
    """The field is called ``key``, which no generic redaction rule catches."""

    import json

    from apps.core.logging import redact_sensitive

    device_key = "JBSWY3DPEHPK3PXP"
    payload = json.dumps({"device": "phone", "key": device_key, "user": "someone@example.com"})

    redacted = redact_sensitive(payload)

    assert device_key not in redacted
    assert "phone" in redacted
    # And in a plain log line, not only in structured output.
    assert device_key not in redact_sensitive(f"provisioned device key={device_key}")
