"""Exports (#89, specification 23, 25.5).

An export is the one point where financial history leaves the encrypted store in
readable form. Three things therefore have to hold: amounts survive as integers,
the file disappears on a timer, and nobody else can reach it.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.categorization.models import Category
from apps.core.crypto import encrypt_model_field
from apps.core.errors import ConflictError, ForbiddenError
from apps.core.key_management import get_user_data_key, provision_user_data_key
from apps.core.models import AuditEvent
from apps.reports.exports import (
    ARCHIVE_FORMAT,
    EXPORT_FIELDS,
    MINIMUM_PASSPHRASE_LENGTH,
    ExportError,
    export_root,
    open_archive,
    safe_export_path,
    seal_archive,
)
from apps.reports.models import TransactionExport
from apps.reports.services import (
    available_exports,
    create_export,
    delete_export,
    purge_expired_exports,
    read_export,
)
from apps.transactions.models import CanonicalTransaction
from tests.factories import make_account, make_user

pytestmark = pytest.mark.django_db

_Type = CanonicalTransaction.TransactionType
_Format = TransactionExport.Format
PASSPHRASE = "correct horse battery staple"


@pytest.fixture
def export_paths(tmp_path: Path, settings: Any) -> Path:
    root = tmp_path / "exports"
    settings.EXPORT_TMP_ROOT = str(root)
    settings.DOCUMENT_TMP_ROOT = str(tmp_path / "documents")
    return root


@pytest.fixture
def master_key(tmp_path: Path, settings: Any) -> bytes:
    key = os.urandom(32)
    path = tmp_path / "master.key"
    path.write_text(base64.urlsafe_b64encode(key).decode(), encoding="ascii")
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)
    return key


@pytest.fixture
def owner(master_key: bytes, export_paths: Path) -> Any:
    user = make_user(email="export-owner@example.com")
    provision_user_data_key(user=user, actor=user, master_key=master_key)
    user.last_login = timezone.now()
    user.save(update_fields=["last_login"])
    return user


@pytest.fixture
def data_key(owner: Any, master_key: bytes) -> bytes:
    return get_user_data_key(user=owner, actor=owner, master_key=master_key)


@pytest.fixture
def account(owner: Any) -> Any:
    return make_account(owner, name_encrypted="checking", name_blind_index="export-checking")


def add(
    user: Any,
    account: Any,
    *,
    amount_minor: int = 42_900,
    transaction_type: str = _Type.PURCHASE,
    merchant: str = "이마트",
    category: Category | None = None,
    day: int = 15,
    month: int = 8,
) -> CanonicalTransaction:
    return CanonicalTransaction.objects.create(
        user=user,
        created_by=user,
        financial_account=account,
        occurred_at=date(2026, month, day),
        amount_encrypted=f"{amount_minor}:KRW",
        currency="KRW",
        transaction_type=transaction_type,
        merchant_encrypted=merchant,
        category=category,
    )


def csv_rows(payload: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(payload.decode())))


# ---------------------------------------------------------------------------
# Amounts survive as integers
# ---------------------------------------------------------------------------


def test_a_csv_export_keeps_amounts_in_minor_units(owner: Any, account: Any) -> None:
    """42900, not 429.00: KRW has no minor unit, and rounding is a lie."""

    add(owner, account, amount_minor=42_900)

    record = create_export(user=owner, export_format=_Format.CSV)
    rows = csv_rows(safe_export_path(f"{record.pk}.csv").read_bytes())

    assert rows[0]["amount_minor"] == "42900"
    assert rows[0]["currency"] == "KRW"
    assert "429.00" not in str(rows)


def test_a_csv_export_carries_the_documented_columns(owner: Any, account: Any) -> None:
    add(owner, account)

    record = create_export(user=owner, export_format=_Format.CSV)
    payload = safe_export_path(f"{record.pk}.csv").read_bytes()
    header = next(csv.reader(io.StringIO(payload.decode())))

    assert tuple(header) == EXPORT_FIELDS


def test_a_json_export_explains_itself(owner: Any, account: Any) -> None:
    """A file found in three years has to say what it is."""

    add(owner, account)

    record = create_export(
        user=owner,
        export_format=_Format.JSON,
        start=date(2026, 8, 1),
        end=date(2026, 8, 31),
    )
    document = json.loads(safe_export_path(f"{record.pk}.json").read_bytes())

    assert document["format"] == ARCHIVE_FORMAT
    assert document["fields"] == list(EXPORT_FIELDS)
    assert "minor units" in document["amounts"]
    assert document["period_start"] == "2026-08-01"
    assert document["row_count"] == 1
    assert isinstance(document["transactions"][0]["amount_minor"], int)


def test_an_export_reports_which_bucket_a_type_lands_in(owner: Any, account: Any) -> None:
    add(owner, account, transaction_type=_Type.CREDIT_CARD_PAYMENT)

    record = create_export(user=owner, export_format=_Format.JSON)
    document = json.loads(safe_export_path(f"{record.pk}.json").read_bytes())

    assert document["transactions"][0]["reporting_bucket"] == "neutral"


def test_encrypted_values_are_decrypted_into_the_export(
    owner: Any, account: Any, data_key: bytes
) -> None:
    transaction = add(owner, account, merchant="")
    transaction.merchant_encrypted = encrypt_model_field(
        transaction, "merchant_encrypted", "스타벅스", key=data_key, key_version=1
    )
    transaction.amount_encrypted = encrypt_model_field(
        transaction, "amount_encrypted", "4200:KRW", key=data_key, key_version=1
    )
    transaction.save(update_fields=["merchant_encrypted", "amount_encrypted"])

    record = create_export(user=owner, export_format=_Format.CSV, data_key=data_key)
    rows = csv_rows(safe_export_path(f"{record.pk}.csv").read_bytes())

    assert rows[0]["merchant"] == "스타벅스"
    assert rows[0]["amount_minor"] == "4200"


def test_a_period_narrows_the_export(owner: Any, account: Any) -> None:
    add(owner, account, day=2)
    add(owner, account, month=9, day=2)

    record = create_export(
        user=owner,
        export_format=_Format.CSV,
        start=date(2026, 8, 1),
        end=date(2026, 8, 31),
    )

    assert record.row_count == 1


# ---------------------------------------------------------------------------
# The encrypted archive
# ---------------------------------------------------------------------------


def test_an_archive_round_trips_with_its_passphrase() -> None:
    sealed = seal_archive(b'{"hello": "world"}', passphrase=PASSPHRASE)

    assert open_archive(sealed, passphrase=PASSPHRASE) == b'{"hello": "world"}'


def test_an_archive_hides_its_contents() -> None:
    sealed = seal_archive(b"merchant: starbucks", passphrase=PASSPHRASE)

    assert b"starbucks" not in sealed
    assert sealed.startswith(ARCHIVE_FORMAT.encode())


def test_the_wrong_passphrase_is_refused() -> None:
    sealed = seal_archive(b"secret", passphrase=PASSPHRASE)

    with pytest.raises(ExportError):
        open_archive(sealed, passphrase="a completely different one")


def test_an_altered_archive_fails_to_open() -> None:
    """Authenticated encryption: a tampered byte is a refusal, not a wrong value."""

    sealed = bytearray(seal_archive(b"secret", passphrase=PASSPHRASE))
    sealed[-1] ^= 0xFF

    with pytest.raises(ExportError):
        open_archive(bytes(sealed), passphrase=PASSPHRASE)


def test_a_file_that_is_not_an_archive_is_refused() -> None:
    with pytest.raises(ExportError):
        open_archive(b"just some bytes", passphrase=PASSPHRASE)


def test_a_truncated_archive_is_refused() -> None:
    with pytest.raises(ExportError):
        open_archive(ARCHIVE_FORMAT.encode() + b"\nshort", passphrase=PASSPHRASE)


def test_a_short_passphrase_is_refused() -> None:
    with pytest.raises(ExportError):
        seal_archive(b"secret", passphrase="a" * (MINIMUM_PASSPHRASE_LENGTH - 1))


def test_two_archives_of_the_same_data_differ() -> None:
    """A fresh salt and nonce each time, so identical exports are not linkable."""

    first = seal_archive(b"same", passphrase=PASSPHRASE)
    second = seal_archive(b"same", passphrase=PASSPHRASE)

    assert first != second


def test_an_encrypted_export_is_unreadable_on_disk_and_opens_with_the_passphrase(
    owner: Any, account: Any
) -> None:
    add(owner, account, merchant="스타벅스")

    record = create_export(user=owner, export_format=_Format.ENCRYPTED, passphrase=PASSPHRASE)
    payload = safe_export_path(f"{record.pk}.encrypted").read_bytes()

    assert record.is_encrypted
    assert "스타벅스".encode() not in payload
    document = json.loads(open_archive(payload, passphrase=PASSPHRASE))
    assert document["transactions"][0]["merchant"] == "스타벅스"


def test_an_encrypted_export_needs_a_passphrase(owner: Any, account: Any) -> None:
    add(owner, account)

    with pytest.raises(ExportError):
        create_export(user=owner, export_format=_Format.ENCRYPTED)


# ---------------------------------------------------------------------------
# Recent authentication
# ---------------------------------------------------------------------------


def test_a_stale_sign_in_cannot_export(owner: Any, account: Any) -> None:
    """An abandoned session must not be enough to dump a whole history."""

    add(owner, account)
    owner.last_login = timezone.now() - timedelta(days=2)
    owner.save(update_fields=["last_login"])

    with pytest.raises(ForbiddenError):
        create_export(user=owner, export_format=_Format.CSV)

    assert not TransactionExport.objects.filter(user=owner).exists()


def test_a_user_who_never_signed_in_cannot_export(owner: Any, account: Any) -> None:
    add(owner, account)
    owner.last_login = None
    owner.save(update_fields=["last_login"])

    with pytest.raises(ForbiddenError):
        create_export(user=owner, export_format=_Format.CSV)


def test_the_window_is_configurable(owner: Any, account: Any, settings: Any) -> None:
    add(owner, account)
    settings.EXPORT_RECENT_AUTH_MAX_AGE_SECONDS = 1
    owner.last_login = timezone.now() - timedelta(seconds=30)
    owner.save(update_fields=["last_login"])

    with pytest.raises(ForbiddenError):
        create_export(user=owner, export_format=_Format.CSV)


# ---------------------------------------------------------------------------
# Expiry and deletion
# ---------------------------------------------------------------------------


def test_an_export_is_created_with_an_expiry(owner: Any, account: Any) -> None:
    add(owner, account)

    record = create_export(user=owner, export_format=_Format.CSV)

    assert record.expires_at > record.created_at
    assert record.is_available


def test_an_expired_export_cannot_be_downloaded(owner: Any, account: Any) -> None:
    add(owner, account)
    record = create_export(user=owner, export_format=_Format.CSV)
    TransactionExport.objects.filter(pk=record.pk).update(
        expires_at=timezone.now() - timedelta(minutes=1)
    )

    with pytest.raises(ConflictError):
        read_export(record.pk, user=owner)


def test_purging_removes_the_file_of_an_expired_export(owner: Any, account: Any) -> None:
    """It goes whether or not anybody downloaded it."""

    add(owner, account)
    record = create_export(user=owner, export_format=_Format.CSV)
    path = safe_export_path(f"{record.pk}.csv")
    TransactionExport.objects.filter(pk=record.pk).update(
        expires_at=timezone.now() - timedelta(minutes=1)
    )

    removed = purge_expired_exports()

    record.refresh_from_db()
    assert removed == 1
    assert not path.exists()
    assert record.deleted_at is not None


def test_purging_leaves_a_live_export_alone(owner: Any, account: Any) -> None:
    add(owner, account)
    record = create_export(user=owner, export_format=_Format.CSV)

    assert purge_expired_exports() == 0
    assert safe_export_path(f"{record.pk}.csv").exists()


def test_purging_is_safe_to_repeat(owner: Any, account: Any) -> None:
    add(owner, account)
    record = create_export(user=owner, export_format=_Format.CSV)
    TransactionExport.objects.filter(pk=record.pk).update(
        expires_at=timezone.now() - timedelta(minutes=1)
    )

    assert purge_expired_exports() == 1
    assert purge_expired_exports() == 0


def test_the_management_command_purges(owner: Any, account: Any) -> None:
    add(owner, account)
    record = create_export(user=owner, export_format=_Format.CSV)
    TransactionExport.objects.filter(pk=record.pk).update(
        expires_at=timezone.now() - timedelta(minutes=1)
    )

    call_command("purge_expired_exports")

    assert not safe_export_path(f"{record.pk}.csv").exists()


def test_an_export_can_be_deleted_early(owner: Any, account: Any) -> None:
    add(owner, account)
    record = create_export(user=owner, export_format=_Format.CSV)
    path = safe_export_path(f"{record.pk}.csv")

    delete_export(record.pk, user=owner)

    record.refresh_from_db()
    assert not path.exists()
    assert record.deleted_at is not None
    assert not record.is_available


def test_a_deleted_export_cannot_be_downloaded(owner: Any, account: Any) -> None:
    add(owner, account)
    record = create_export(user=owner, export_format=_Format.CSV)
    delete_export(record.pk, user=owner)

    with pytest.raises(ConflictError):
        read_export(record.pk, user=owner)


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


def test_another_users_export_cannot_be_downloaded(
    owner: Any, account: Any, master_key: bytes
) -> None:
    add(owner, account)
    record = create_export(user=owner, export_format=_Format.CSV)
    stranger = make_user(email="export-stranger@example.com")
    provision_user_data_key(user=stranger, actor=stranger, master_key=master_key)

    with pytest.raises(ForbiddenError):
        read_export(record.pk, user=stranger)


def test_another_users_export_cannot_be_deleted(
    owner: Any, account: Any, master_key: bytes
) -> None:
    add(owner, account)
    record = create_export(user=owner, export_format=_Format.CSV)
    stranger = make_user(email="export-vandal@example.com")

    with pytest.raises(ForbiddenError):
        delete_export(record.pk, user=stranger)

    assert safe_export_path(f"{record.pk}.csv").exists()


def test_an_export_only_contains_its_owners_transactions(
    owner: Any, account: Any, master_key: bytes
) -> None:
    add(owner, account, merchant="mine")
    stranger = make_user(email="export-onlooker@example.com")
    stranger.last_login = timezone.now()
    stranger.save(update_fields=["last_login"])
    add(stranger, make_account(stranger, name_blind_index="export-theirs"), merchant="theirs")

    record = create_export(user=owner, export_format=_Format.CSV)
    payload = safe_export_path(f"{record.pk}.csv").read_bytes()

    assert b"mine" in payload
    assert b"theirs" not in payload
    assert record.row_count == 1


def test_available_exports_are_scoped_to_their_owner(owner: Any, account: Any) -> None:
    add(owner, account)
    create_export(user=owner, export_format=_Format.CSV)
    stranger = make_user(email="export-lister@example.com")

    assert available_exports(owner).count() == 1
    assert available_exports(stranger).count() == 0


# ---------------------------------------------------------------------------
# Storage safety
# ---------------------------------------------------------------------------


def test_the_export_root_is_private(owner: Any, account: Any) -> None:
    add(owner, account)
    create_export(user=owner, export_format=_Format.CSV)

    assert oct(export_root().stat().st_mode)[-3:] == "700"


def test_an_export_file_is_readable_only_by_its_owner_process(owner: Any, account: Any) -> None:
    add(owner, account)
    record = create_export(user=owner, export_format=_Format.CSV)

    mode = oct(safe_export_path(f"{record.pk}.csv").stat().st_mode)[-3:]

    assert mode == "600"


def test_a_path_outside_the_export_root_is_refused() -> None:
    with pytest.raises(ExportError):
        safe_export_path("../escaped.csv")


# ---------------------------------------------------------------------------
# Auditing
# ---------------------------------------------------------------------------


def test_creating_an_export_is_audited_without_recording_any_value(
    owner: Any, account: Any
) -> None:
    add(owner, account, merchant="스타벅스", amount_minor=4_200)

    record = create_export(
        user=owner,
        export_format=_Format.CSV,
        start=date(2026, 8, 1),
        end=date(2026, 8, 31),
    )

    event = AuditEvent.objects.filter(user=owner, event_type="export_created").first()
    assert event is not None
    assert event.metadata["format"] == "csv"
    assert event.metadata["row_count"] == 1
    assert event.metadata["period_start"] == "2026-08-01"
    assert "스타벅스" not in str(event.metadata)
    assert "4200" not in str(event.metadata)
    assert record.row_count == 1


def test_downloading_an_export_is_audited(owner: Any, account: Any) -> None:
    add(owner, account)
    record = create_export(user=owner, export_format=_Format.CSV)

    read_export(record.pk, user=owner)

    record.refresh_from_db()
    assert record.downloaded_at is not None
    assert AuditEvent.objects.filter(user=owner, event_type="export_downloaded").exists()


# ---------------------------------------------------------------------------
# The pages
# ---------------------------------------------------------------------------


def test_the_page_generates_and_lists_an_export(owner: Any, account: Any) -> None:
    add(owner, account)
    client = Client()
    client.force_login(owner)

    created = client.post(reverse("report-exports"), data={"export_format": _Format.CSV.value})
    listed = client.get(reverse("report-exports"))

    assert created.status_code == 302
    assert listed.context["exports"].count() == 1


def test_the_page_refuses_an_encrypted_export_without_a_passphrase(
    owner: Any, account: Any
) -> None:
    add(owner, account)
    client = Client()
    client.force_login(owner)

    response = client.post(
        reverse("report-exports"), data={"export_format": _Format.ENCRYPTED.value}
    )

    assert response.status_code == 400
    assert not TransactionExport.objects.filter(user=owner).exists()


def test_downloading_streams_the_file_as_an_attachment(owner: Any, account: Any) -> None:
    add(owner, account, amount_minor=42_900)
    record = create_export(user=owner, export_format=_Format.CSV)
    client = Client()
    client.force_login(owner)

    response = client.get(reverse("export-download", kwargs={"pk": record.pk}))

    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv"
    assert "attachment" in response["Content-Disposition"]
    assert "no-store" in response["Cache-Control"]
    assert b"42900" in response.content


def test_downloading_another_users_export_redirects_without_the_file(
    owner: Any, account: Any, master_key: bytes
) -> None:
    add(owner, account, merchant="mine")
    record = create_export(user=owner, export_format=_Format.CSV)
    stranger = make_user(email="export-thief@example.com")
    provision_user_data_key(user=stranger, actor=stranger, master_key=master_key)
    client = Client()
    client.force_login(stranger)

    response = client.get(reverse("export-download", kwargs={"pk": record.pk}))

    assert response.status_code == 302
    assert b"mine" not in response.content


def test_the_export_pages_require_a_login(owner: Any, account: Any) -> None:
    add(owner, account)
    record = create_export(user=owner, export_format=_Format.CSV)

    for url in (
        reverse("report-exports"),
        reverse("export-download", kwargs={"pk": record.pk}),
    ):
        response = Client().get(url)
        assert response.status_code == 302
        assert reverse("login") in response.headers["Location"]
