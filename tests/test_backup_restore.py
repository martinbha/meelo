"""Backup, restore, and recovery (#96, specification 28).

A backup nobody has restored is a hypothesis. These tests restore one — into a
disposable directory, checked against its own manifest — and then prove the thing
that actually matters: that a transaction written before the backup is still
readable after it, with the master key that was kept somewhere else.
"""

from __future__ import annotations

import base64
import json
import os
import tarfile
from datetime import date, timedelta
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.migrations.recorder import MigrationRecorder
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django_otp.oath import TOTP
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.core.backup import (
    BACKUP_FORMAT,
    DATABASE_NAME,
    MANIFEST_NAME,
    BackupError,
    assert_master_key_separate,
    create_backup,
    read_manifest,
    unpack_backup,
    verify_backup,
)
from apps.core.crypto import encrypt_model_field, read_model_field
from apps.core.key_management import get_user_data_key, provision_user_data_key
from apps.core.management.commands.create_backup import PASSPHRASE_ENV
from apps.reports.exports import open_archive, seal_archive
from apps.transactions.models import CanonicalTransaction
from apps.transactions.money import read_money, store_money
from apps.users.models import RecoveryCode, User
from apps.users.recovery import consume_recovery_code, regenerate_recovery_codes
from tests.factories import make_account, make_user

pytestmark = pytest.mark.django_db

PASSPHRASE = "a long enough backup passphrase"
MERCHANT = "스타벅스 강남점"


@pytest.fixture
def master_key(tmp_path: Path, settings: Any) -> bytes:
    key = os.urandom(32)
    path = tmp_path / "master.key"
    path.write_text(base64.urlsafe_b64encode(key).decode(), encoding="ascii")
    path.chmod(0o600)
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)
    settings.DOCUMENT_TMP_ROOT = str(tmp_path / "documents")
    return key


@pytest.fixture
def owner(master_key: bytes) -> Any:
    user = make_user(email="backup-owner@example.com")
    provision_user_data_key(user=user, actor=user, master_key=master_key)
    return user


@pytest.fixture
def data_key(owner: Any, master_key: bytes) -> bytes:
    return get_user_data_key(user=owner, actor=owner, master_key=master_key)


@pytest.fixture
def documents(tmp_path: Path) -> Path:
    root = tmp_path / "documents"
    root.mkdir(parents=True, exist_ok=True)
    (root / "screenshot-one.enc").write_bytes(b"already-encrypted-at-rest")
    (root / "nested").mkdir(exist_ok=True)
    (root / "nested" / "screenshot-two.enc").write_bytes(b"also-encrypted")
    return root


def add(owner: Any, account: Any, data_key: bytes, *, amount_minor: int = 42_900) -> Any:
    from apps.core.value_objects import Money

    transaction = CanonicalTransaction.objects.create(
        user=owner,
        created_by=owner,
        financial_account=account,
        occurred_at=date(2026, 8, 15),
        amount_encrypted="1:KRW",
        currency="KRW",
        transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
    )
    store_money(transaction, "amount_encrypted", Money(amount_minor, "KRW"), data_key=data_key)
    transaction.merchant_encrypted = encrypt_model_field(
        transaction, "merchant_encrypted", MERCHANT, key=data_key, key_version=1
    )
    transaction.save(update_fields=["amount_encrypted", "merchant_encrypted"])
    return transaction


# ---------------------------------------------------------------------------
# The archive
# ---------------------------------------------------------------------------


def test_a_backup_round_trips(owner: Any, data_key: bytes, tmp_path: Path) -> None:
    account = make_account(owner, name_blind_index="backup-account")
    add(owner, account, data_key)
    archive = tmp_path / "backup.enc"

    manifest = create_backup(archive, passphrase=PASSPHRASE)
    report = unpack_backup(archive, passphrase=PASSPHRASE, destination=tmp_path / "out")

    assert manifest.format == BACKUP_FORMAT
    assert manifest.row_counts["otp_totp.totpdevice"] == 0
    assert manifest.row_counts["otp_static.staticdevice"] == 0
    assert manifest.row_counts["otp_static.statictoken"] == 0
    assert report.is_clean
    assert report.database_path.exists()
    assert verify_backup(archive, passphrase=PASSPHRASE) == []


def test_the_archive_is_unreadable_without_the_passphrase(
    owner: Any, data_key: bytes, tmp_path: Path
) -> None:
    account = make_account(owner, name_blind_index="backup-account")
    add(owner, account, data_key)
    archive = tmp_path / "backup.enc"
    create_backup(archive, passphrase=PASSPHRASE)

    with pytest.raises(BackupError):
        read_manifest(archive, passphrase="something else entirely")


def test_a_tampered_archive_is_refused(owner: Any, data_key: bytes, tmp_path: Path) -> None:
    account = make_account(owner, name_blind_index="backup-account")
    add(owner, account, data_key)
    archive = tmp_path / "backup.enc"
    create_backup(archive, passphrase=PASSPHRASE)
    payload = bytearray(archive.read_bytes())
    payload[-1] ^= 0xFF
    archive.write_bytes(bytes(payload))

    with pytest.raises(BackupError):
        read_manifest(archive, passphrase=PASSPHRASE)


def test_the_archive_is_written_private(owner: Any, tmp_path: Path) -> None:
    archive = tmp_path / "backup.enc"

    create_backup(archive, passphrase=PASSPHRASE)

    assert oct(archive.stat().st_mode)[-3:] == "600"


# ---------------------------------------------------------------------------
# The master key is kept out
# ---------------------------------------------------------------------------


def test_the_master_key_is_never_in_the_archive(
    owner: Any, data_key: bytes, master_key: bytes, tmp_path: Path, settings: Any
) -> None:
    """An archive containing both is a plaintext backup with extra steps."""

    account = make_account(owner, name_blind_index="backup-account")
    add(owner, account, data_key)
    archive = tmp_path / "backup.enc"

    manifest = create_backup(archive, passphrase=PASSPHRASE)
    body = open_archive(archive.read_bytes(), passphrase=PASSPHRASE)

    assert manifest.includes_master_key is False
    assert master_key not in body
    assert base64.urlsafe_b64encode(master_key) not in body
    assert Path(settings.FIELD_ENCRYPTION_MASTER_KEY_FILE).name.encode() not in body


def test_the_manifest_says_the_key_is_elsewhere(owner: Any, tmp_path: Path) -> None:
    """A restore that cannot find a key should know why."""

    archive = tmp_path / "backup.enc"
    create_backup(archive, passphrase=PASSPHRASE)
    body = open_archive(archive.read_bytes(), passphrase=PASSPHRASE)

    import io

    with tarfile.open(fileobj=io.BytesIO(body)) as opened:
        member = opened.extractfile("manifest.json")
        assert member is not None
        payload = json.loads(member.read())

    assert payload["includes_master_key"] is False
    assert "stored separately" in payload["master_key_note"]


def test_an_archive_claiming_to_hold_the_key_is_refused(owner: Any, tmp_path: Path) -> None:
    """Ours never do, so a file that says otherwise is not ours."""

    import io

    from apps.reports.exports import seal_archive

    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        body = json.dumps({"format": BACKUP_FORMAT, "includes_master_key": True}).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(body)
        archive.addfile(info, io.BytesIO(body))
    forged = tmp_path / "forged.enc"
    forged.write_bytes(seal_archive(payload.getvalue(), passphrase=PASSPHRASE))

    with pytest.raises(BackupError, match="master key"):
        read_manifest(forged, passphrase=PASSPHRASE)


# ---------------------------------------------------------------------------
# What is backed up
# ---------------------------------------------------------------------------


def test_retained_screenshots_travel_with_the_backup(
    owner: Any, data_key: bytes, documents: Path, tmp_path: Path
) -> None:
    account = make_account(owner, name_blind_index="backup-account")
    add(owner, account, data_key)
    archive = tmp_path / "backup.enc"

    manifest = create_backup(archive, passphrase=PASSPHRASE, document_root=documents)
    report = unpack_backup(archive, passphrase=PASSPHRASE, destination=tmp_path / "out")

    assert manifest.document_count == 2
    restored = {path.name for path in report.document_paths if path.is_file()}
    assert restored == {"screenshot-one.enc", "screenshot-two.enc"}
    assert (tmp_path / "out" / "documents" / "screenshot-one.enc").read_bytes() == (
        b"already-encrypted-at-rest"
    )


def test_the_manifest_records_the_schema_and_the_rows(
    owner: Any, data_key: bytes, tmp_path: Path
) -> None:
    account = make_account(owner, name_blind_index="backup-account")
    add(owner, account, data_key)
    archive = tmp_path / "backup.enc"

    manifest = create_backup(archive, passphrase=PASSPHRASE)

    assert manifest.migration_count > 0
    assert manifest.latest_migrations["transactions"]
    assert manifest.row_counts["transactions.canonicaltransaction"] == 1
    assert manifest.row_counts["users.userdatakey"] == 1


def test_the_manifest_records_the_retention_class(owner: Any, tmp_path: Path) -> None:
    archive = tmp_path / "weekly.enc"

    manifest = create_backup(archive, passphrase=PASSPHRASE, retention_label="weekly")

    assert manifest.retention_label == "weekly"
    assert read_manifest(archive, passphrase=PASSPHRASE).retention_label == "weekly"


def test_key_material_and_key_files_are_rejected_from_database_backups(
    master_key: bytes, tmp_path: Path
) -> None:
    with pytest.raises(BackupError, match="master-key material"):
        assert_master_key_separate(master_key, master_key=master_key)

    import io

    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        member = tarfile.TarInfo("secrets/master.key")
        member.size = len(master_key)
        archive.addfile(member, io.BytesIO(master_key))

    with pytest.raises(BackupError, match="master-key file"):
        assert_master_key_separate(payload.getvalue())


def test_a_truncated_archive_is_caught_by_its_counts(
    owner: Any, data_key: bytes, documents: Path, tmp_path: Path
) -> None:
    """A missing file is caught by the count rather than a silent half-restore."""

    account = make_account(owner, name_blind_index="backup-account")
    add(owner, account, data_key)
    archive = tmp_path / "backup.enc"
    create_backup(archive, passphrase=PASSPHRASE, document_root=documents)
    destination = tmp_path / "out"
    unpack_backup(archive, passphrase=PASSPHRASE, destination=destination)
    (destination / "documents" / "screenshot-one.enc").unlink()

    # Re-checking the unpacked tree against the manifest reports the loss.
    manifest = read_manifest(archive, passphrase=PASSPHRASE)
    remaining = [p for p in (destination / "documents").rglob("*") if p.is_file()]

    assert len(remaining) != manifest.document_count


def test_an_archive_entry_cannot_escape_the_destination(tmp_path: Path) -> None:
    """An archive is untrusted input even when it is one we wrote."""

    import io

    from apps.reports.exports import seal_archive

    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        body = json.dumps(
            {
                "format": BACKUP_FORMAT,
                "created_at": "2026-08-15T00:00:00+00:00",
                "migration_count": 0,
                "latest_migrations": {},
                "row_counts": {},
                "document_count": 0,
                "includes_master_key": False,
            }
        ).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(body)
        archive.addfile(info, io.BytesIO(body))
        escape = tarfile.TarInfo("../escaped.txt")
        escape.size = 3
        archive.addfile(escape, io.BytesIO(b"bad"))
    hostile = tmp_path / "hostile.enc"
    hostile.write_bytes(seal_archive(payload.getvalue(), passphrase=PASSPHRASE))

    with pytest.raises(BackupError, match="escapes"):
        unpack_backup(hostile, passphrase=PASSPHRASE, destination=tmp_path / "out")


# ---------------------------------------------------------------------------
# History is readable after a restore
# ---------------------------------------------------------------------------


def test_a_restored_transaction_is_still_readable_with_the_separate_key(
    owner: Any, data_key: bytes, master_key: bytes, tmp_path: Path
) -> None:
    """The point of the whole exercise."""

    account = make_account(owner, name_blind_index="backup-account")
    original = add(owner, account, data_key, amount_minor=42_900)
    archive = tmp_path / "backup.enc"
    create_backup(archive, passphrase=PASSPHRASE)

    # A disposable environment: unpack, then wipe and reload the rows.
    report = unpack_backup(archive, passphrase=PASSPHRASE, destination=tmp_path / "out")
    CanonicalTransaction.objects.all().delete()
    assert not CanonicalTransaction.objects.exists()

    call_command("loaddata", str(report.database_path), verbosity=0)

    restored = CanonicalTransaction.objects.get(pk=original.pk)
    assert read_money(restored, "amount_encrypted", data_key=data_key).amount_minor == 42_900
    assert read_model_field(restored, "merchant_encrypted", key=data_key) == MERCHANT


def test_the_restored_rows_are_still_ciphertext_on_disk(
    owner: Any, data_key: bytes, tmp_path: Path
) -> None:
    """A backup must not be the place encryption quietly stops."""

    account = make_account(owner, name_blind_index="backup-account")
    add(owner, account, data_key)
    archive = tmp_path / "backup.enc"
    create_backup(archive, passphrase=PASSPHRASE)

    report = unpack_backup(archive, passphrase=PASSPHRASE, destination=tmp_path / "out")
    exported = report.database_path.read_text()

    assert MERCHANT not in exported
    assert "42900:KRW" not in exported


# ---------------------------------------------------------------------------
# The commands
# ---------------------------------------------------------------------------


def run(command: str, *args: Any, **options: Any) -> str:
    out = StringIO()
    call_command(command, *args, stdout=out, stderr=StringIO(), **options)
    return out.getvalue()


def legacy_archive_without_otp_state(
    source: Path, destination: Path, *, pre_mfa_schema: bool
) -> Path:
    """Rewrite a synthetic archive as one emitted before OTP rows were backed up."""

    body = open_archive(source.read_bytes(), passphrase=PASSPHRASE)
    with tarfile.open(fileobj=BytesIO(body), mode="r:gz") as archive:
        manifest_file = archive.extractfile(MANIFEST_NAME)
        database_file = archive.extractfile(DATABASE_NAME)
        assert manifest_file is not None
        assert database_file is not None
        manifest = json.load(manifest_file)
        rows = json.load(database_file)

    otp_models = {
        "otp_totp.totpdevice",
        "otp_static.staticdevice",
        "otp_static.statictoken",
    }
    manifest["row_counts"] = {
        model: count for model, count in manifest["row_counts"].items() if model not in otp_models
    }
    rows = [row for row in rows if row.get("model") not in otp_models]
    if pre_mfa_schema:
        otp_migrations = [
            app for app in ("otp_totp", "otp_static") if app in manifest["latest_migrations"]
        ]
        manifest["migration_count"] -= sum(
            1 for migration in MigrationRecorder.Migration.objects.filter(app__in=otp_migrations)
        )
        for app in otp_migrations:
            manifest["latest_migrations"].pop(app)

    legacy_payload = BytesIO()
    with tarfile.open(fileobj=legacy_payload, mode="w:gz") as archive:
        for name, value in ((MANIFEST_NAME, manifest), (DATABASE_NAME, rows)):
            encoded = json.dumps(value, ensure_ascii=False, indent=2).encode()
            member = tarfile.TarInfo(name)
            member.size = len(encoded)
            member.mode = 0o600
            archive.addfile(member, BytesIO(encoded))
    destination.write_bytes(seal_archive(legacy_payload.getvalue(), passphrase=PASSPHRASE))
    return destination


def test_enrolled_mfa_and_recovery_state_survive_encrypted_restore(
    owner: Any,
    master_key: bytes,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Restore the actual logical archive, then authenticate with saved MFA state."""

    totp_key = "0123456789abcdef0123456789abcdef01234567"
    fixed_time = 1_800_000_005
    current_counter = fixed_time // 30
    device = TOTPDevice.objects.create(
        user=owner,
        name="synthetic-authenticator",
        key=totp_key,
        confirmed=True,
        last_t=current_counter - 1,
        tolerance=0,
        throttling_failure_count=1,
        throttling_failure_timestamp=timezone.now() - timedelta(minutes=10),
    )
    device_id = device.pk
    recovery_codes = regenerate_recovery_codes(owner)
    consumed_code, remaining_code = recovery_codes[:2]
    assert consume_recovery_code(owner, consumed_code)
    static_device = StaticDevice.objects.create(user=owner, name="synthetic-static", confirmed=True)
    StaticToken.objects.create(device=static_device, token="SYNTHETIC")

    archive = tmp_path / "mfa-backup.enc"
    manifest = create_backup(archive, passphrase=PASSPHRASE, master_key=master_key)
    assert manifest.row_counts["otp_totp.totpdevice"] == 1
    assert manifest.row_counts["otp_static.staticdevice"] == 1
    assert manifest.row_counts["otp_static.statictoken"] == 1
    assert manifest.row_counts["users.recoverycode"] == 10
    assert verify_backup(archive, passphrase=PASSPHRASE, master_key=master_key) == []

    # The file itself stays sealed even though the TOTP seed and static token
    # must be present in the decrypted logical database export.
    sealed = archive.read_bytes()
    assert totp_key.encode() not in sealed
    assert b"SYNTHETIC" not in sealed

    monkeypatch.setenv(PASSPHRASE_ENV, PASSPHRASE)
    owner_id = owner.pk
    owner.delete()
    assert not User.objects.filter(pk=owner_id).exists()

    restore_output = run(
        "restore_backup",
        str(archive),
        str(tmp_path / "restored-mfa"),
        load=True,
    )
    assert "Loaded" in restore_output

    restored_user = User.objects.get(pk=owner_id)
    restored_device = TOTPDevice.objects.get(pk=device_id, user=restored_user)
    assert restored_device.key == totp_key
    assert restored_device.confirmed is True
    assert restored_device.last_t == current_counter - 1
    assert restored_device.throttling_failure_count == 1
    assert StaticDevice.objects.filter(user=restored_user, confirmed=True).count() == 1
    assert StaticToken.objects.filter(device__user=restored_user, token="SYNTHETIC").count() == 1
    assert RecoveryCode.objects.filter(user=restored_user, used_at__isnull=True).count() == 9
    assert RecoveryCode.objects.filter(user=restored_user, used_at__isnull=False).count() == 1

    client = Client()
    login = client.post(
        reverse("login"),
        {"username": restored_user.email, "password": "password"},
    )
    assert login["Location"] == reverse("two-factor-verify")
    clock = TOTP(
        restored_device.bin_key,
        restored_device.step,
        restored_device.t0,
        restored_device.digits,
        restored_device.drift,
    )
    clock.time = fixed_time
    token = str(clock.token()).zfill(restored_device.digits)
    with patch("django_otp.plugins.otp_totp.models.time.time", return_value=fixed_time):
        assert client.post(reverse("two-factor-verify"), {"token": token}).status_code == 302
    assert client.get(reverse("transaction-list")).status_code == 200

    restored_device.refresh_from_db()
    assert restored_device.last_t == current_counter
    assert restored_device.throttling_failure_count == 0
    assert consume_recovery_code(restored_user, consumed_code) is False
    assert consume_recovery_code(restored_user, remaining_code) is True
    assert consume_recovery_code(restored_user, remaining_code) is False

    replay_client = Client()
    replay_client.force_login(restored_user)
    with patch("django_otp.plugins.otp_totp.models.time.time", return_value=fixed_time):
        replay = replay_client.post(reverse("two-factor-verify"), {"token": token})
    assert replay.status_code == 400
    restored_device.refresh_from_db()
    assert restored_device.last_t == current_counter


def test_legacy_archive_with_otp_migrations_but_no_state_fails_closed(
    owner: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """An old post-MFA archive cannot silently make an account password-only."""

    device = TOTPDevice.objects.create(user=owner, name="synthetic-authenticator", confirmed=True)
    source = tmp_path / "current-backup.enc"
    create_backup(source, passphrase=PASSPHRASE)
    legacy = legacy_archive_without_otp_state(
        source, tmp_path / "legacy-after-mfa-schema.enc", pre_mfa_schema=False
    )

    problems = verify_backup(legacy, passphrase=PASSPHRASE)
    assert len(problems) == 1
    assert "otp_totp.totpdevice" in problems[0]
    assert "otp_static.staticdevice" in problems[0]
    assert "otp_static.statictoken" in problems[0]
    assert device.key not in problems[0]

    report = unpack_backup(legacy, passphrase=PASSPHRASE, destination=tmp_path / "legacy-out")
    assert not report.is_clean

    monkeypatch.setenv(PASSPHRASE_ENV, PASSPHRASE)
    with pytest.raises(CommandError, match="does not match its manifest"):
        run(
            "restore_backup",
            str(legacy),
            str(tmp_path / "legacy-restore"),
            load=True,
        )
    assert User.objects.filter(pk=owner.pk).exists()
    assert TOTPDevice.objects.filter(pk=device.pk).exists()


def test_pre_mfa_archive_without_otp_migrations_remains_verifiable(
    owner: Any, tmp_path: Path
) -> None:
    """Archives made before OTP tables existed remain readable and verifiable."""

    TOTPDevice.objects.create(user=owner, name="synthetic-authenticator", confirmed=True)
    source = tmp_path / "current-backup.enc"
    create_backup(source, passphrase=PASSPHRASE)
    legacy = legacy_archive_without_otp_state(
        source, tmp_path / "legacy-before-mfa-schema.enc", pre_mfa_schema=True
    )

    assert verify_backup(legacy, passphrase=PASSPHRASE) == []


def test_the_create_command_requires_a_passphrase_from_the_environment(
    owner: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """Not an argument: that puts it in shell history and the process list."""

    monkeypatch.delenv(PASSPHRASE_ENV, raising=False)

    with pytest.raises(CommandError, match=PASSPHRASE_ENV):
        run("create_backup", str(tmp_path / "backup.enc"))


def test_the_commands_write_verify_and_restore(
    owner: Any, data_key: bytes, tmp_path: Path, monkeypatch: Any
) -> None:
    account = make_account(owner, name_blind_index="backup-account")
    add(owner, account, data_key)
    monkeypatch.setenv(PASSPHRASE_ENV, PASSPHRASE)
    archive = tmp_path / "backup.enc"

    created = run("create_backup", str(archive), skip_documents=True)
    verified = run("verify_backup", str(archive))
    restored = run("restore_backup", str(archive), str(tmp_path / "out"))

    assert "row(s)" in created
    assert "master key is not in this archive" in created
    assert "verified" in verified
    assert "Nothing was loaded" in restored


def test_the_restore_command_loads_when_asked(
    owner: Any, data_key: bytes, tmp_path: Path, monkeypatch: Any
) -> None:
    account = make_account(owner, name_blind_index="backup-account")
    original = add(owner, account, data_key)
    monkeypatch.setenv(PASSPHRASE_ENV, PASSPHRASE)
    archive = tmp_path / "backup.enc"
    run("create_backup", str(archive), skip_documents=True)
    CanonicalTransaction.objects.all().delete()

    output = run(
        "restore_backup",
        str(archive),
        str(tmp_path / "out"),
        load=True,
        allow_non_empty=True,
    )

    assert "Loaded" in output
    assert CanonicalTransaction.objects.filter(pk=original.pk).exists()


def test_the_restore_command_refuses_a_non_empty_database(
    owner: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    make_account(owner, name_blind_index="backup-account")
    monkeypatch.setenv(PASSPHRASE_ENV, PASSPHRASE)
    archive = tmp_path / "backup.enc"
    run("create_backup", str(archive), skip_documents=True)

    with pytest.raises(CommandError, match="not empty"):
        run("restore_backup", str(archive), str(tmp_path / "out"), load=True)


def test_the_verify_command_fails_loudly_on_a_bad_archive(
    owner: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv(PASSPHRASE_ENV, PASSPHRASE)
    archive = tmp_path / "backup.enc"
    archive.write_bytes(b"not an archive at all")

    with pytest.raises(BackupError):
        run("verify_backup", str(archive))


def test_an_archive_is_decrypted_once_per_operation(
    owner: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """Opening one costs an Argon2id derivation. Doing it twice is a waste."""

    from apps.core import backup as backup_module

    archive = tmp_path / "backup.enc"
    create_backup(archive, passphrase=PASSPHRASE)
    opens = {"count": 0}
    original = backup_module._open

    def counted(path: Path, *, passphrase: str) -> bytes:
        opens["count"] += 1
        return original(path, passphrase=passphrase)

    monkeypatch.setattr(backup_module, "_open", counted)

    unpack_backup(archive, passphrase=PASSPHRASE, destination=tmp_path / "out")

    assert opens["count"] == 1

    opens["count"] = 0
    assert verify_backup(archive, passphrase=PASSPHRASE) == []
    assert opens["count"] == 1
