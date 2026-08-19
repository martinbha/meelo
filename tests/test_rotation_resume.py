"""A rotation that stops halfway and one that is read through (#167, specification 22.6).

Three claims:

- resuming does not redo finished rows, and does not walk the whole history to
  find where it stopped,
- a read arriving mid-rotation succeeds, whichever version the row is under,
- and the old key is only retired once verification finds nothing left on it.

The correctness of resuming has never depended on the checkpoint — the envelope
version already makes a finished row skippable. What the checkpoint buys is the
difference between minutes and hours on the rotation that actually matters, so
these tests check both: that the work is not redone, and that the walk does not
start from the beginning.
"""

from __future__ import annotations

import base64
import os
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command

from apps.core.crypto import envelope_key_version
from apps.core.key_management import (
    get_user_data_key,
    get_user_search_key,
    provision_user_data_key,
)
from apps.core.models import RotationCheckpoint
from apps.core.rotation import EncryptedModel, resume_point, rotate_model, rotate_user
from apps.core.value_objects import Money
from apps.transactions.models import CanonicalTransaction
from apps.transactions.money import store_money
from apps.users.models import UserDataKey
from tests.factories import make_account, make_user

pytestmark = pytest.mark.django_db

MERCHANT = "스타벅스 강남점"
TRANSACTIONS = EncryptedModel(
    "transactions.CanonicalTransaction",
    ("amount_encrypted", "merchant_encrypted"),
    (("merchant_blind_index", "merchant_encrypted", "merchant"),),
)


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
    user = make_user(email="resume-owner@example.com")
    provision_user_data_key(user=user, actor=user, master_key=master_key)
    return user


@pytest.fixture
def data_key(owner: Any, master_key: bytes) -> bytes:
    return get_user_data_key(user=owner, actor=owner, master_key=master_key)


@pytest.fixture
def search_key(owner: Any, master_key: bytes) -> bytes:
    return get_user_search_key(user=owner, actor=owner, master_key=master_key)


def add_rows(owner: Any, data_key: bytes, count: int) -> list[CanonicalTransaction]:
    account = make_account(owner, name_blind_index="resume-account")
    rows = []
    for index in range(count):
        transaction = CanonicalTransaction(
            user=owner,
            created_by=owner,
            financial_account=account,
            occurred_at=date(2026, 8, 1 + index % 28),
            currency="KRW",
            transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
        )
        transaction.amount_encrypted = "1:KRW"
        transaction.save()
        store_money(transaction, "amount_encrypted", Money(1_000 + index, "KRW"), data_key=data_key)
        transaction.encrypt_fields({"merchant_encrypted": MERCHANT}, key=data_key, key_version=1)
        transaction.save(update_fields=["amount_encrypted", "merchant_encrypted"])
        rows.append(transaction)
    return rows


def versions(owner: Any) -> set[int]:
    return {
        envelope_key_version(value)
        for value in CanonicalTransaction.objects.filter(user=owner).values_list(
            "merchant_encrypted", flat=True
        )
    }


# ----------------------------------------------------------------------
# Resuming
# ----------------------------------------------------------------------


def test_a_completed_rotation_records_a_checkpoint(
    owner: Any, data_key: bytes, search_key: bytes
) -> None:
    add_rows(owner, data_key, 5)

    rotate_model(
        TRANSACTIONS,
        user=owner,
        old_key=data_key,
        new_key=os.urandom(32),
        new_version=2,
        search_key=search_key,
        batch_size=2,
    )

    checkpoint = RotationCheckpoint.objects.get(
        user=owner, key_version=2, model_label=TRANSACTIONS.label
    )
    assert checkpoint.is_complete
    assert checkpoint.rows_rotated == 5


def test_an_interrupted_rotation_resumes_after_its_checkpoint(
    owner: Any, data_key: bytes, search_key: bytes
) -> None:
    """The walk starts after the last finished row, not at the first one."""

    rows = sorted(add_rows(owner, data_key, 6), key=lambda row: row.pk)
    new_key = os.urandom(32)
    # Stand in for a crash: the first three moved, and the checkpoint says so.
    for row in rows[:3]:
        row.encrypt_fields(
            {
                "merchant_encrypted": MERCHANT,
                "amount_encrypted": row.read_field("amount_encrypted", key=data_key),
            },
            key=new_key,
            key_version=2,
        )
        row.save(update_fields=["merchant_encrypted", "amount_encrypted"])
    RotationCheckpoint.objects.create(
        user=owner,
        key_version=2,
        model_label=TRANSACTIONS.label,
        last_record_id=str(rows[2].pk),
        rows_rotated=3,
    )

    report = rotate_model(
        TRANSACTIONS,
        user=owner,
        old_key=data_key,
        new_key=new_key,
        new_version=2,
        search_key=search_key,
        batch_size=2,
    )

    # Only the remaining three were even looked at — the finished rows were not
    # re-read, which is the whole point of the checkpoint.
    assert report.rows_examined == 3
    assert report.rows_rewritten == 3


def test_resuming_is_still_correct_when_the_checkpoint_is_missing(
    owner: Any, data_key: bytes, search_key: bytes
) -> None:
    """The version check is what guarantees correctness; the checkpoint is speed."""

    rows = sorted(add_rows(owner, data_key, 4), key=lambda row: row.pk)
    new_key = os.urandom(32)
    for row in rows[:2]:
        # Both fields, because a row is only finished when every field on it is.
        row.encrypt_fields(
            {
                "merchant_encrypted": MERCHANT,
                "amount_encrypted": row.read_field("amount_encrypted", key=data_key),
            },
            key=new_key,
            key_version=2,
        )
        row.save(update_fields=["merchant_encrypted", "amount_encrypted"])

    report = rotate_model(
        TRANSACTIONS,
        user=owner,
        old_key=data_key,
        new_key=new_key,
        new_version=2,
        search_key=search_key,
    )

    # Everything is walked, because there is no checkpoint to start after.
    assert report.rows_examined == 4
    # But only the two that still needed it were rewritten.
    assert report.rows_rewritten == 2


def test_a_checkpoint_from_another_rotation_is_ignored(owner: Any) -> None:
    RotationCheckpoint.objects.create(
        user=owner, key_version=5, model_label=TRANSACTIONS.label, last_record_id="anything"
    )

    assert resume_point(user=owner, new_version=2, label=TRANSACTIONS.label) == ""


def test_a_completed_checkpoint_does_not_skip_a_rerun(owner: Any) -> None:
    """A finished rotation re-run must examine everything, not resume past it."""

    from django.utils import timezone

    RotationCheckpoint.objects.create(
        user=owner,
        key_version=2,
        model_label=TRANSACTIONS.label,
        last_record_id="something",
        completed_at=timezone.now(),
    )

    assert resume_point(user=owner, new_version=2, label=TRANSACTIONS.label) == ""


# ----------------------------------------------------------------------
# Reading during the window
# ----------------------------------------------------------------------


def test_a_row_still_on_the_old_key_is_readable_mid_rotation(
    owner: Any, data_key: bytes, search_key: bytes, master_key: bytes
) -> None:
    """Both versions are live between the key switch and the last row moving."""

    rows = sorted(add_rows(owner, data_key, 4), key=lambda row: row.pk)
    new_version = 2
    new_key = os.urandom(32)
    UserDataKey.objects.filter(user=owner, is_active=True).update(is_active=False)
    from apps.core.key_management import wrap_data_key

    UserDataKey.objects.create(
        user=owner,
        version=new_version,
        wrapped_key=wrap_data_key(
            new_key, master_key=master_key, user_id=owner.pk, version=new_version
        ),
        is_active=True,
    )
    owner.encryption_key_version = new_version
    owner.save(update_fields=["encryption_key_version"])
    # Half the rows have moved; half have not.
    for row in rows[:2]:
        row.encrypt_fields({"merchant_encrypted": MERCHANT}, key=new_key, key_version=new_version)
        row.save(update_fields=["merchant_encrypted"])

    assert versions(owner) == {1, 2}
    for row in rows:
        row.refresh_from_db()
        assert row.read_field("merchant_encrypted", key=new_key) == MERCHANT


def test_a_value_readable_under_no_key_still_fails_loudly(owner: Any, data_key: bytes) -> None:
    """The fallback must not turn a corrupt row into a silent one."""

    from apps.core.crypto import InvalidCiphertextError

    rows = add_rows(owner, data_key, 1)
    row = rows[0]
    row.merchant_encrypted = row.merchant_encrypted[:-6] + "AAAAAA"
    row.save(update_fields=["merchant_encrypted"])

    with pytest.raises(InvalidCiphertextError):
        row.read_field("merchant_encrypted", key=data_key)


# ----------------------------------------------------------------------
# Dry run and retirement
# ----------------------------------------------------------------------


def test_a_dry_run_writes_nothing_and_creates_no_key(
    owner: Any, data_key: bytes, search_key: bytes, capsys: Any
) -> None:
    add_rows(owner, data_key, 3)
    before = {row.pk: row.merchant_encrypted for row in CanonicalTransaction.objects.all()}

    call_command("rotate_encryption_keys", email=owner.email, dry_run=True)

    assert UserDataKey.objects.filter(user=owner).count() == 1
    assert not RotationCheckpoint.objects.exists()
    after = {row.pk: row.merchant_encrypted for row in CanonicalTransaction.objects.all()}
    assert after == before
    assert "Nothing was written" in capsys.readouterr().out


def test_a_dry_run_at_the_module_level_reports_without_writing(
    owner: Any, data_key: bytes, search_key: bytes
) -> None:
    add_rows(owner, data_key, 3)

    report = rotate_user(
        user=owner,
        old_key=data_key,
        new_key=os.urandom(32),
        new_version=2,
        search_key=search_key,
        models=[TRANSACTIONS],
        dry_run=True,
    )

    assert report.rows_rewritten == 3
    assert versions(owner) == {1}
    assert not RotationCheckpoint.objects.exists()


def test_the_old_key_is_retired_only_after_a_clean_verification(
    owner: Any, data_key: bytes, capsys: Any
) -> None:
    add_rows(owner, data_key, 3)

    call_command("rotate_encryption_keys", email=owner.email)
    assert UserDataKey.objects.filter(user=owner).count() == 2

    call_command("rotate_encryption_keys", email=owner.email, retire=True)

    remaining = UserDataKey.objects.filter(user=owner)
    assert remaining.count() == 1
    assert remaining.get().is_active
    assert "none remaining at any earlier one" in capsys.readouterr().out
