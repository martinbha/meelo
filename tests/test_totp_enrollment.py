import base64
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.core.models import AuditEvent
from tests.factories import make_user

pytestmark = pytest.mark.django_db
PASSWORD = "totp-enrollment-password"


@pytest.fixture
def owner() -> Any:
    return make_user(email="totp-enrollment@example.com", password=PASSWORD)


@pytest.fixture
def signed_in(owner: Any) -> Client:
    client = Client()
    client.force_login(owner)
    return client


def test_enrollment_renders_local_qr_and_confirms_current_code(
    owner: Any, signed_in: Client
) -> None:
    response = signed_in.get(reverse("totp-enroll"))
    device = TOTPDevice.objects.get(user=owner, confirmed=False)

    assert response.status_code == 200
    assert b"<svg" in response.content
    manual_secret = base64.b32encode(device.bin_key).decode("ascii").rstrip("=")
    assert manual_secret.encode() in response.content

    response = signed_in.post(reverse("totp-enroll"), {"token": str(totp(device.bin_key)).zfill(6)})

    assert response.status_code == 302
    device.refresh_from_db()
    assert device.confirmed is True
    assert AuditEvent.objects.filter(user=owner, event_type="two_factor_enabled").exists()


def test_wrong_confirmation_code_leaves_device_inactive(owner: Any, signed_in: Client) -> None:
    signed_in.get(reverse("totp-enroll"))
    response = signed_in.post(reverse("totp-enroll"), {"token": "000000"})

    assert response.status_code == 400
    assert TOTPDevice.objects.get(user=owner).confirmed is False


def test_disabling_requires_current_password(owner: Any, signed_in: Client) -> None:
    TOTPDevice.objects.create(user=owner, name="phone", confirmed=True)

    assert signed_in.post(reverse("totp-disable"), {"password": "wrong"}).status_code == 400
    assert TOTPDevice.objects.filter(user=owner).exists()

    response = signed_in.post(reverse("totp-disable"), {"password": PASSWORD})
    assert response.status_code == 302
    assert not TOTPDevice.objects.filter(user=owner).exists()
    assert AuditEvent.objects.filter(user=owner, event_type="two_factor_disabled").exists()
