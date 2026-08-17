from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

import pytest

from apps.core.errors import ForbiddenError
from apps.core.key_management import (
    KeyManagementError,
    get_user_data_key,
    load_master_key,
    provision_user_data_key,
    unwrap_data_key,
    wrap_data_key,
)
from apps.core.models import AuditEvent
from apps.users.models import UserDataKey


@pytest.fixture
def user(db: Any) -> Any:
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user("key-owner@example.com", password="password")


def test_master_key_file_requires_valid_external_key(tmp_path: Path) -> None:
    path = tmp_path / "master-key"
    master_key = os.urandom(32)
    path.write_text(base64.urlsafe_b64encode(master_key).decode(), encoding="ascii")

    assert load_master_key(path) == master_key
    with pytest.raises(KeyManagementError, match="cannot be read"):
        load_master_key(tmp_path / "missing")
    path.write_text("not-a-key", encoding="ascii")
    with pytest.raises(KeyManagementError, match="base64"):
        load_master_key(path)


def test_wrapped_key_authenticates_user_and_version() -> None:
    master_key = os.urandom(32)
    data_key = os.urandom(32)
    envelope = wrap_data_key(data_key, master_key=master_key, user_id="user-1", version=2)

    assert unwrap_data_key(envelope, master_key=master_key, user_id="user-1", version=2) == data_key
    with pytest.raises(KeyManagementError, match="authentication"):
        unwrap_data_key(envelope, master_key=master_key, user_id="user-2", version=2)
    with pytest.raises(KeyManagementError, match="invalid"):
        unwrap_data_key(envelope, master_key=master_key, user_id="user-1", version=3)


@pytest.mark.django_db
def test_provision_and_access_store_only_wrapped_user_key(user: Any) -> None:
    master_key = os.urandom(32)
    record = provision_user_data_key(user=user, actor=user, master_key=master_key)
    again = provision_user_data_key(user=user, actor=user, master_key=master_key)
    data_key = get_user_data_key(user=user, actor=user, master_key=master_key)

    assert again.pk == record.pk
    assert len(data_key) == 32
    assert record.wrapped_key != base64.urlsafe_b64encode(data_key).decode()
    assert UserDataKey.objects.filter(user=user).count() == 1
    assert list(user.audit_events.values_list("event_type", flat=True)) == [
        AuditEvent.EventType.ENCRYPTION_KEY_PROVISIONED,
        AuditEvent.EventType.ENCRYPTION_KEY_ACCESSED,
    ]


@pytest.mark.django_db
def test_user_key_access_rejects_other_users_and_wrong_master(user: Any) -> None:
    other = type(user).objects.create_user("other-key@example.com", password="password")
    master_key = os.urandom(32)
    provision_user_data_key(user=user, actor=user, master_key=master_key)

    with pytest.raises(ForbiddenError):
        get_user_data_key(user=user, actor=other, master_key=master_key)
    with pytest.raises(KeyManagementError, match="authentication"):
        get_user_data_key(user=user, actor=user, master_key=os.urandom(32))


def test_the_search_key_is_derived_rather_than_stored() -> None:
    """Every caller holding the data key reaches the same search key."""

    from apps.core.key_management import derive_blind_index_key

    data_key = os.urandom(32)

    assert derive_blind_index_key(data_key) == derive_blind_index_key(data_key)


def test_the_search_key_is_not_the_data_key() -> None:
    """An index leak must not hand over the plaintext with it."""

    from apps.core.key_management import derive_blind_index_key

    data_key = os.urandom(32)
    derived = derive_blind_index_key(data_key)

    assert derived != data_key
    assert len(derived) == 32
    assert derived != derive_blind_index_key(os.urandom(32))


def test_a_malformed_data_key_cannot_derive_a_search_key() -> None:
    from apps.core.key_management import KeyManagementError, derive_blind_index_key

    with pytest.raises(KeyManagementError):
        derive_blind_index_key(b"short")
