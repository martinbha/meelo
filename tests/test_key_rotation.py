"""Key rotation and verification (#94, specification 22.6, 25.4).

Rotation is a long operation over data a person cannot afford to lose, so what
these tests care about is what happens when it stops halfway — and whether
anything is retired before it has been read back.
"""

from __future__ import annotations

import base64
import os
from datetime import date
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command

from apps.categorization.models import Category
from apps.categorization.normalization import merchant_blind_index
from apps.core.crypto import encrypt_model_field, envelope_key_version, read_model_field
from apps.core.key_management import (
    get_user_data_key,
    get_user_search_key,
    provision_user_data_key,
)
from apps.core.models import AuditEvent
from apps.core.rotation import (
    ENCRYPTED_MODELS,
    EncryptedModel,
    rotate_user,
    verify_user,
)
from apps.reports.spending import monthly_spending
from apps.transactions.models import CanonicalTransaction
from apps.transactions.money import store_money
from apps.users.models import UserDataKey
from tests.factories import make_account, make_user

pytestmark = pytest.mark.django_db

MERCHANT = "스타벅스 강남점"


def search_key_for(user: Any) -> bytes:
    """One user's stored blind-index key, read the way production reads it.

    Derived from the master key rather than from the data key, so rotating the
    encryption key leaves it alone — which is what these tests check.
    """

    from apps.core.key_management import load_master_key

    return get_user_search_key(user=user, actor=user, master_key=load_master_key())


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
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)
    settings.DOCUMENT_TMP_ROOT = str(tmp_path / "documents")
    return key


@pytest.fixture
def owner(master_key: bytes) -> Any:
    user = make_user(email="rotation-owner@example.com")
    provision_user_data_key(user=user, actor=user, master_key=master_key)
    return user


@pytest.fixture
def data_key(owner: Any, master_key: bytes) -> bytes:
    return get_user_data_key(user=owner, actor=owner, master_key=master_key)


@pytest.fixture
def account(owner: Any) -> Any:
    return make_account(owner, name_blind_index="rotation-account")


def add(
    owner: Any, account: Any, data_key: bytes, *, amount_minor: int = 42_900, day: int = 15
) -> CanonicalTransaction:
    from apps.core.value_objects import Money

    transaction = CanonicalTransaction.objects.create(
        user=owner,
        created_by=owner,
        financial_account=account,
        occurred_at=date(2026, 8, day),
        amount_encrypted="1:KRW",
        currency="KRW",
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
        merchant_blind_index=merchant_blind_index(
            MERCHANT,
            user_id=owner.pk,
            key=search_key_for(owner),
        ),
    )
    store_money(transaction, "amount_encrypted", Money(amount_minor, "KRW"), data_key=data_key)
    transaction.merchant_encrypted = encrypt_model_field(
        transaction, "merchant_encrypted", MERCHANT, key=data_key, key_version=1
    )
    transaction.save(update_fields=["amount_encrypted", "merchant_encrypted"])
    return transaction


# ---------------------------------------------------------------------------
# The field registry
# ---------------------------------------------------------------------------


def test_every_encrypted_column_is_registered_for_rotation() -> None:
    """A field missing here is one rotation walks past and a key deletion loses."""

    from django.apps import apps

    registered = {
        f"{spec.label.lower()}.{name}" for spec in ENCRYPTED_MODELS for name in spec.fields
    }
    actual = {
        f"{model._meta.app_label}.{model._meta.model_name}.{field.name}"
        for model in apps.get_models()
        for field in model._meta.get_fields()
        if getattr(field, "name", "").endswith("_encrypted")
    }
    # Exports and audit metadata hold no per-user encrypted columns.
    assert actual - registered == set(), sorted(actual - registered)


def test_every_registered_model_resolves() -> None:
    for spec in ENCRYPTED_MODELS:
        assert spec.model() is not None


# ---------------------------------------------------------------------------
# Moving values
# ---------------------------------------------------------------------------


def test_rotation_re_encrypts_under_the_new_version(
    owner: Any, account: Any, data_key: bytes
) -> None:
    transaction = add(owner, account, data_key)
    new_key = os.urandom(32)

    report = rotate_user(
        user=owner,
        old_key=data_key,
        new_key=new_key,
        new_version=2,
        search_key=search_key_for(owner),
        models=[TRANSACTIONS],
    )

    transaction.refresh_from_db()
    assert report.succeeded
    assert report.fields_rewritten == 2
    assert envelope_key_version(transaction.amount_encrypted) == 2
    assert read_model_field(transaction, "merchant_encrypted", key=new_key) == MERCHANT


def test_the_old_key_no_longer_opens_a_rotated_row(
    owner: Any, account: Any, data_key: bytes
) -> None:
    from apps.core.crypto import InvalidCiphertextError

    transaction = add(owner, account, data_key)
    new_key = os.urandom(32)
    rotate_user(
        user=owner,
        old_key=data_key,
        new_key=new_key,
        new_version=2,
        search_key=search_key_for(owner),
        models=[TRANSACTIONS],
    )

    transaction.refresh_from_db()
    with pytest.raises(InvalidCiphertextError):
        read_model_field(transaction, "merchant_encrypted", key=data_key)


def test_rotating_the_encryption_key_leaves_the_search_key_alone(
    owner: Any, account: Any, data_key: bytes
) -> None:
    """Two keys, two rotations. Moving one must not silently move the other.

    The search key used to be derived from the data key, so rotating the
    encryption key rebuilt every blind index as a side effect — an index
    rebuild nobody asked for, hidden inside an operation about something else.
    Now the search key comes from the master key and stays put, so exact-match
    lookups keep working across an encryption rotation. Rotating the *search*
    key, and the reindex that goes with it, is #168.
    """

    transaction = add(owner, account, data_key)
    before = transaction.merchant_blind_index
    new_key = os.urandom(32)

    rotate_user(
        user=owner,
        old_key=data_key,
        new_key=new_key,
        new_version=2,
        search_key=search_key_for(owner),
        models=[TRANSACTIONS],
    )

    transaction.refresh_from_db()
    assert transaction.merchant_blind_index == before
    # And the value it indexes is genuinely under the new key, so this is not
    # simply a rotation that did nothing.
    assert transaction.merchant_blind_index == merchant_blind_index(
        MERCHANT,
        user_id=owner.pk,
        key=search_key_for(owner),
    )
    assert read_model_field(transaction, "merchant_encrypted", key=new_key) == MERCHANT


def test_another_users_rows_are_never_touched(
    owner: Any, account: Any, data_key: bytes, master_key: bytes
) -> None:
    stranger = make_user(email="rotation-stranger@example.com")
    provision_user_data_key(user=stranger, actor=stranger, master_key=master_key)
    their_key = get_user_data_key(user=stranger, actor=stranger, master_key=master_key)
    theirs = add(stranger, make_account(stranger, name_blind_index="rotation-theirs"), their_key)
    new_key = os.urandom(32)

    rotate_user(
        user=owner,
        old_key=data_key,
        new_key=new_key,
        new_version=2,
        search_key=search_key_for(owner),
        models=[TRANSACTIONS],
    )

    theirs.refresh_from_db()
    assert envelope_key_version(theirs.amount_encrypted) == 1
    assert read_model_field(theirs, "merchant_encrypted", key=their_key) == MERCHANT


# ---------------------------------------------------------------------------
# Interruption and resumption
# ---------------------------------------------------------------------------


def test_an_interrupted_rotation_resumes_by_running_again(
    owner: Any, account: Any, data_key: bytes
) -> None:
    """No cursor to corrupt: the envelopes say what is left."""

    rows = [add(owner, account, data_key, day=index + 1) for index in range(6)]
    new_key = os.urandom(32)
    search_key = search_key_for(owner)

    # A run that dies after the first batch of two.
    from apps.core import rotation as rotation_module

    original = rotation_module._batches
    calls = {"count": 0}

    def one_batch_then_stop(queryset: Any, size: int) -> Any:
        for batch in original(queryset, size):
            calls["count"] += 1
            yield batch
            if calls["count"] >= 1:
                return

    rotation_module._batches = one_batch_then_stop
    try:
        first = rotate_user(
            user=owner,
            old_key=data_key,
            new_key=new_key,
            new_version=2,
            search_key=search_key,
            batch_size=2,
            models=[TRANSACTIONS],
        )
    finally:
        rotation_module._batches = original

    assert first.rows_rewritten == 2

    # Running again finishes the job and does not redo the first two.
    second = rotate_user(
        user=owner,
        old_key=data_key,
        new_key=new_key,
        new_version=2,
        search_key=search_key,
        batch_size=2,
        models=[TRANSACTIONS],
    )

    assert second.rows_rewritten == 4
    for row in rows:
        row.refresh_from_db()
        assert envelope_key_version(row.amount_encrypted) == 2


def test_rotating_an_already_rotated_row_changes_nothing(
    owner: Any, account: Any, data_key: bytes
) -> None:
    transaction = add(owner, account, data_key)
    new_key = os.urandom(32)
    search_key = search_key_for(owner)
    common = {
        "user": owner,
        "old_key": data_key,
        "new_key": new_key,
        "new_version": 2,
        "search_key": search_key,
        "models": [TRANSACTIONS],
    }
    rotate_user(**common)
    transaction.refresh_from_db()
    sealed = transaction.amount_encrypted

    again = rotate_user(**common)

    transaction.refresh_from_db()
    assert again.rows_rewritten == 0
    assert transaction.amount_encrypted == sealed


def test_a_row_written_after_the_new_key_went_active_is_left_alone(
    owner: Any, account: Any, data_key: bytes
) -> None:
    """Writes during a rotation already land under the key it is moving toward."""

    new_key = os.urandom(32)
    fresh = add(owner, account, new_key)
    CanonicalTransaction.objects.filter(pk=fresh.pk).update(
        amount_encrypted=encrypt_model_field(
            fresh, "amount_encrypted", "999:KRW", key=new_key, key_version=2
        )
    )

    report = rotate_user(
        user=owner,
        old_key=data_key,
        new_key=new_key,
        new_version=2,
        search_key=search_key_for(owner),
        models=[EncryptedModel("transactions.CanonicalTransaction", ("amount_encrypted",))],
    )

    fresh.refresh_from_db()
    assert report.rows_rewritten == 0
    assert read_model_field(fresh, "amount_encrypted", key=new_key) == "999:KRW"


# ---------------------------------------------------------------------------
# Verification before anything is retired
# ---------------------------------------------------------------------------


def test_verification_passes_after_a_complete_rotation(
    owner: Any, account: Any, data_key: bytes
) -> None:
    add(owner, account, data_key)
    new_key = os.urandom(32)
    rotate_user(
        user=owner,
        old_key=data_key,
        new_key=new_key,
        new_version=2,
        search_key=search_key_for(owner),
        models=[TRANSACTIONS],
    )

    report = verify_user(user=owner, key=new_key, expected_version=2, models=[TRANSACTIONS])

    assert report.is_clean
    assert report.values_checked == 2


def test_verification_names_a_row_rotation_has_not_reached(
    owner: Any, account: Any, data_key: bytes
) -> None:
    """Still readable under the old key — so retiring it now would lose the row."""

    add(owner, account, data_key)
    new_key = os.urandom(32)

    report = verify_user(user=owner, key=new_key, expected_version=2, models=[TRANSACTIONS])

    assert not report.is_clean
    assert report.stale_versions
    assert not report.unreadable
    assert "version 1" in report.stale_versions[0]


def test_verification_separates_unreadable_from_merely_stale(
    owner: Any, account: Any, data_key: bytes
) -> None:
    """Different problems: one is data loss, the other is unfinished work."""

    transaction = add(owner, account, data_key)
    corrupted = transaction.amount_encrypted[:-4] + "AAAA"
    CanonicalTransaction.objects.filter(pk=transaction.pk).update(amount_encrypted=corrupted)

    report = verify_user(user=owner, key=data_key, expected_version=1, models=[TRANSACTIONS])

    assert report.unreadable
    assert "amount_encrypted" in report.unreadable[0]


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def rotate_command(**options: Any) -> str:
    out = StringIO()
    call_command("rotate_encryption_keys", stdout=out, stderr=StringIO(), **options)
    return out.getvalue()


def test_the_command_rotates_and_keeps_the_old_key_until_asked(
    owner: Any, account: Any, data_key: bytes
) -> None:
    add(owner, account, data_key)

    output = rotate_command(email=owner.email)

    assert "v1 -> v2" in output
    assert UserDataKey.objects.filter(user=owner).count() == 2
    assert UserDataKey.objects.get(user=owner, is_active=True).version == 2


def test_the_command_retires_only_after_a_clean_verification(
    owner: Any, account: Any, data_key: bytes
) -> None:
    add(owner, account, data_key)

    output = rotate_command(email=owner.email, retire=True)

    assert "retired 1 superseded key" in output
    assert UserDataKey.objects.filter(user=owner).count() == 1
    assert UserDataKey.objects.get(user=owner).version == 2


def test_the_command_audits_the_rotation(owner: Any, account: Any, data_key: bytes) -> None:
    add(owner, account, data_key)

    rotate_command(email=owner.email)

    event = AuditEvent.objects.filter(user=owner, event_type="encryption_key_rotated").first()
    assert event is not None
    assert event.metadata == {"from_version": 1, "to_version": 2}


def test_the_command_verifies_without_changing_anything(
    owner: Any, account: Any, data_key: bytes
) -> None:
    transaction = add(owner, account, data_key)
    sealed = transaction.amount_encrypted

    output = rotate_command(email=owner.email, verify_only=True)

    transaction.refresh_from_db()
    assert "checked" in output
    assert transaction.amount_encrypted == sealed
    assert UserDataKey.objects.filter(user=owner).count() == 1


def test_the_command_refuses_an_unknown_user(master_key: bytes) -> None:
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        rotate_command(email="nobody@example.com")


# ---------------------------------------------------------------------------
# Everything still works afterwards
# ---------------------------------------------------------------------------


def test_reports_and_search_survive_a_full_rotation(
    owner: Any, account: Any, data_key: bytes, master_key: bytes
) -> None:
    """The figures a person looks at must not move because a key did."""

    for index in range(4):
        add(owner, account, data_key, amount_minor=10_000 + index, day=index + 1)
    Category.objects.create(
        user=owner,
        name_encrypted="food",
        name_blind_index="rotation-food",
        category_type=Category.CategoryType.EXPENSE,
    )
    before = monthly_spending(owner, year=2026, month=8, data_key=data_key).totals("KRW")

    rotate_command(email=owner.email, retire=True)

    rotated_key = get_user_data_key(user=owner, actor=owner, master_key=master_key)
    after = monthly_spending(owner, year=2026, month=8, data_key=rotated_key).totals("KRW")

    assert after == before
    assert rotated_key != data_key
    # And search still finds the merchant, under the new search key.
    expected = merchant_blind_index(
        MERCHANT,
        user_id=owner.pk,
        key=search_key_for(owner),
    )
    assert (
        CanonicalTransaction.objects.filter(user=owner, merchant_blind_index=expected).count() == 4
    )


def test_an_export_still_reads_after_a_rotation(
    owner: Any, account: Any, data_key: bytes, master_key: bytes, tmp_path: Path, settings: Any
) -> None:
    from django.utils import timezone

    from apps.reports.exports import safe_export_path
    from apps.reports.models import TransactionExport
    from apps.reports.services import create_export

    settings.EXPORT_TMP_ROOT = str(tmp_path / "exports")
    add(owner, account, data_key, amount_minor=42_900)
    owner.last_login = timezone.now()
    owner.save(update_fields=["last_login"])

    rotate_command(email=owner.email, retire=True)
    rotated_key = get_user_data_key(user=owner, actor=owner, master_key=master_key)

    record = create_export(
        user=owner, export_format=TransactionExport.Format.CSV, data_key=rotated_key
    )
    payload = safe_export_path(f"{record.pk}.csv").read_bytes()

    assert b"42900" in payload
    assert MERCHANT.encode() in payload


def test_batches_hold_one_chunk_at_a_time(owner: Any, account: Any, data_key: bytes) -> None:
    """Bounded batches has to mean bounded memory, not just bounded transactions."""

    from apps.core.rotation import _batches

    for index in range(7):
        add(owner, account, data_key, day=index + 1)
    queryset = CanonicalTransaction.objects.filter(user_id=owner.pk)

    sizes = [len(batch) for batch in _batches(queryset, 3)]

    assert sizes == [3, 3, 1]
    # And every row is visited exactly once.
    seen = [row.pk for batch in _batches(queryset, 3) for row in batch]
    assert len(seen) == len(set(seen)) == 7
