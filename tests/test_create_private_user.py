"""The first command a new operator runs (#100, specification 27).

Registration is closed, so this command is the only way an account comes into
existence. It has to produce an account that actually works, which means a data
key as well as a password: every amount, merchant, and account name is encrypted
under one, and a user without one cannot store anything at all.
"""

from __future__ import annotations

import base64
import os
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import CommandError, call_command

from apps.core.key_management import get_user_data_key
from apps.core.models import AuditEvent
from apps.users.models import User, UserDataKey

pytestmark = pytest.mark.django_db


@pytest.fixture
def master_key(tmp_path: Path, settings: Any) -> bytes:
    key = os.urandom(32)
    path = tmp_path / "master.key"
    path.write_text(base64.urlsafe_b64encode(key).decode(), encoding="ascii")
    path.chmod(0o600)
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)
    return key


@pytest.fixture
def password(monkeypatch: Any) -> str:
    secret = "a-long-enough-passphrase"
    monkeypatch.setattr("getpass.getpass", lambda *args, **kwargs: secret)
    return secret


def test_the_new_account_can_store_something(
    master_key: bytes, password: str, monkeypatch: Any
) -> None:
    """An account without a data key fails on its owner's first upload."""

    call_command("create_private_user", "--email", "owner@example.com", stdout=StringIO())

    user = User.objects.get(email="owner@example.com")
    assert user.check_password(password)
    assert len(get_user_data_key(user=user, actor=user, master_key=master_key)) == 32


def test_provisioning_is_recorded(master_key: bytes, password: str) -> None:
    call_command("create_private_user", "--email", "owner@example.com", stdout=StringIO())

    user = User.objects.get(email="owner@example.com")
    assert UserDataKey.objects.filter(user=user, is_active=True).count() == 1
    assert AuditEvent.objects.filter(user=user, event_type="encryption_key_provisioned").exists()


def test_no_account_is_created_when_the_master_key_is_unreadable(
    settings: Any, password: str, tmp_path: Path
) -> None:
    """Failing here beats failing on the owner's first upload."""

    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(tmp_path / "absent.key")

    with pytest.raises(CommandError, match="master key"):
        call_command("create_private_user", "--email", "owner@example.com", stdout=StringIO())

    assert not User.objects.filter(email="owner@example.com").exists()


def test_a_duplicate_email_is_refused(master_key: bytes, password: str) -> None:
    call_command("create_private_user", "--email", "owner@example.com", stdout=StringIO())

    with pytest.raises(CommandError, match="already exists"):
        call_command("create_private_user", "--email", "OWNER@example.com", stdout=StringIO())
