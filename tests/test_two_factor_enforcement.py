from typing import Any

import pytest
from django.core.cache import cache
from django.test import Client, override_settings
from django.urls import reverse
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.core.models import AuditEvent
from apps.users.recovery import regenerate_recovery_codes
from tests.factories import make_user

pytestmark = pytest.mark.django_db
PASSWORD = "two-factor-enforcement-password"


@pytest.fixture(autouse=True)
def clear_cache() -> None:
    cache.clear()


@pytest.fixture
def owner() -> Any:
    return make_user(email="enforced@example.com", password=PASSWORD)


def test_enrolled_user_must_verify_before_authenticated_or_admin_pages(owner: Any) -> None:
    owner.is_staff = True
    owner.save(update_fields=("is_staff",))
    device = TOTPDevice.objects.create(user=owner, name="phone", confirmed=True)
    client = Client()
    client.force_login(owner)

    for url in (reverse("transaction-list"), reverse("admin:index")):
        response = client.get(url)
        assert response.status_code == 302
        assert response["Location"] == reverse("two-factor-verify")

    code = str(totp(device.bin_key)).zfill(6)
    assert client.post(reverse("two-factor-verify"), {"token": code}).status_code == 302
    assert client.get(reverse("transaction-list")).status_code == 200
    assert client.get(reverse("admin:index")).status_code == 200


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
def test_failed_codes_are_audited_and_throttled(owner: Any) -> None:
    TOTPDevice.objects.create(user=owner, name="phone", confirmed=True)
    client = Client()
    client.force_login(owner)

    for _ in range(5):
        assert client.post(reverse("two-factor-verify"), {"token": "000000"}).status_code == 400
    assert client.post(reverse("two-factor-verify"), {"token": "000000"}).status_code == 429
    assert AuditEvent.objects.filter(user=owner, event_type="two_factor_failure").count() == 5


def test_user_without_device_is_unaffected(owner: Any) -> None:
    client = Client()
    client.force_login(owner)

    assert client.get(reverse("transaction-list")).status_code == 200


def test_login_redirects_enrolled_user_to_verification_and_accepts_recovery_code(
    owner: Any,
) -> None:
    TOTPDevice.objects.create(user=owner, name="phone", confirmed=True)
    recovery_code = regenerate_recovery_codes(owner)[0]
    client = Client()

    response = client.post(reverse("login"), {"username": owner.email, "password": PASSWORD})
    assert response["Location"] == reverse("two-factor-verify")

    response = client.post(reverse("two-factor-verify"), {"token": recovery_code})
    assert response.status_code == 302
    assert client.get(reverse("transaction-list")).status_code == 200
