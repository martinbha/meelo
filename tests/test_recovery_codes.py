from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.users.models import RecoveryCode
from apps.users.recovery import consume_recovery_code, regenerate_recovery_codes
from apps.users.security import security_overview
from tests.factories import make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner() -> Any:
    return make_user(email="recovery@example.com", password="recovery-password")


def test_recovery_code_is_hashed_and_consumed_once(owner: Any) -> None:
    code = regenerate_recovery_codes(owner)[0]
    stored = RecoveryCode.objects.filter(user=owner).first()

    assert stored is not None
    assert code not in stored.code_hash
    assert stored.code_hash.startswith(("pbkdf2_", "argon2$"))
    assert consume_recovery_code(owner, code) is True
    assert consume_recovery_code(owner, code) is False


def test_regeneration_invalidates_old_codes_and_updates_count(owner: Any) -> None:
    old = regenerate_recovery_codes(owner)[0]
    new = regenerate_recovery_codes(owner)

    assert consume_recovery_code(owner, old) is False
    assert security_overview(owner).recovery_codes_remaining == len(new)


def test_codes_are_shown_once_with_acknowledgement(owner: Any) -> None:
    client = Client()
    client.force_login(owner)
    response = client.post(reverse("recovery-codes-regenerate"))
    codes = list(response.context["codes"])

    assert response.status_code == 200
    assert all(code.encode() in response.content for code in codes)
    assert b"I have saved these codes" in response.content
    assert all(code not in client.session.values() for code in codes)
