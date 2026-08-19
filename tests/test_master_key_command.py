"""Preparing and checking key material without a hand-typed OpenSSL line (#166).

Two operations, two failure modes worth being careful about.

Generating is dangerous in one direction only: overwriting a master key does not
replace it, it makes every wrapped key in the database permanently unopenable,
and the rows survive to look fine. So the command refuses, with no flag to make
it not refuse.

Verifying is the check worth running after a restore and before a rotation. A
wrapped key that will not unwrap is not a warning — it is one person's entire
financial history — and the only cheap moment to find out is before anything
depends on it.
"""

from __future__ import annotations

import base64
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from django.core.management import CommandError, call_command

from apps.core.key_management import (
    load_master_key,
    provision_user_data_key,
    wrap_data_key,
)
from apps.core.management.commands.master_key import (
    generate_master_key,
    verify_wrapped_keys,
)
from apps.users.models import UserDataKey, UserSearchKey
from tests.factories import make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def master_key(tmp_path: Path, settings: Any) -> bytes:
    key = os.urandom(32)
    path = tmp_path / "master.key"
    path.write_text(base64.urlsafe_b64encode(key).decode(), encoding="ascii")
    path.chmod(0o600)
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)
    return key


# ----------------------------------------------------------------------
# Generating
# ----------------------------------------------------------------------


def test_a_generated_key_is_usable_without_any_further_steps(
    tmp_path: Path, settings: Any, capsys: Any
) -> None:
    """The point of the command: a fresh deployment with no OpenSSL involved."""

    path = tmp_path / "new" / "master.key"

    call_command("master_key", "generate", path=str(path))
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)

    assert len(load_master_key()) == 32
    assert str(path) in capsys.readouterr().out


def test_a_generated_key_has_the_mode_the_loader_requires(tmp_path: Path) -> None:
    path = tmp_path / "master.key"

    generate_master_key(path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_key_is_never_world_readable_even_for_an_instant(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Writing first and fixing the mode afterwards leaves a window.

    Asserted through the syscall rather than the result: a file created 0644 and
    chmodded to 0600 a microsecond later ends in the same state as this one, and
    only one of them was ever safe.
    """

    path = tmp_path / "master.key"
    modes: list[int] = []
    original = os.open

    def watching(file: Any, flags: int, mode: int = 0o777, **kwargs: Any) -> int:
        if str(file) == str(path):
            modes.append(mode)
        return original(file, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", watching)

    generate_master_key(path)

    assert modes == [0o600]


def test_generating_over_an_existing_key_is_refused(tmp_path: Path) -> None:
    """There is no --force. Overwriting is not replacing; it is destroying."""

    path = tmp_path / "master.key"
    generate_master_key(path)
    original = path.read_bytes()

    with pytest.raises(CommandError, match="permanently unreadable"):
        generate_master_key(path)

    assert path.read_bytes() == original


def test_generate_requires_a_path() -> None:
    with pytest.raises(CommandError, match="requires --path"):
        call_command("master_key", "generate")


def test_the_generated_key_never_appears_on_standard_output(tmp_path: Path, capsys: Any) -> None:
    """A key that has been on a terminal is in a scrollback and a shell history."""

    path = tmp_path / "master.key"

    call_command("master_key", "generate", path=str(path))

    written = path.read_text(encoding="ascii").strip()
    captured = capsys.readouterr()
    assert written not in captured.out
    assert written not in captured.err
    assert base64.urlsafe_b64decode(written).hex() not in captured.out


def test_two_generated_keys_differ(tmp_path: Path) -> None:
    generate_master_key(tmp_path / "one.key")
    generate_master_key(tmp_path / "two.key")

    assert (tmp_path / "one.key").read_bytes() != (tmp_path / "two.key").read_bytes()


# ----------------------------------------------------------------------
# Verifying
# ----------------------------------------------------------------------


def test_verification_passes_when_every_wrapped_key_opens(master_key: bytes, capsys: Any) -> None:
    for index in range(3):
        user = make_user(email=f"verify-{index}@example.com")
        provision_user_data_key(user=user, actor=user, master_key=master_key)

    call_command("master_key", "verify")

    output = capsys.readouterr().out
    assert "Every wrapped key opened." in output
    # Three users, each with a data key and a search key.
    assert "Checked 6 wrapped key(s) across 3 user(s)" in output


def test_verification_names_the_user_whose_key_will_not_open(master_key: bytes) -> None:
    """The one thing worth knowing, and the one thing hardest to find otherwise."""

    good = make_user(email="verify-good@example.com")
    provision_user_data_key(user=good, actor=good, master_key=master_key)
    broken = make_user(email="verify-broken@example.com")
    provision_user_data_key(user=broken, actor=broken, master_key=master_key)
    # Wrapped under a different master key: the row looks entirely normal.
    record = UserDataKey.objects.get(user=broken)
    record.wrapped_key = wrap_data_key(
        os.urandom(32), master_key=os.urandom(32), user_id=broken.pk, version=record.version
    )
    record.save(update_fields=["wrapped_key"])

    report = verify_wrapped_keys(master_key=master_key)

    assert not report.is_clean
    assert len(report.unreadable) == 1
    assert "verify-broken@example.com" in report.unreadable[0]
    assert "data key v1" in report.unreadable[0]
    assert "verify-good@example.com" not in " ".join(report.unreadable)


def test_verification_checks_search_keys_too(master_key: bytes) -> None:
    """A search key that will not open means every lookup silently finds nothing."""

    user = make_user(email="verify-search@example.com")
    provision_user_data_key(user=user, actor=user, master_key=master_key)
    record = UserSearchKey.objects.get(user=user)
    record.wrapped_key = wrap_data_key(
        os.urandom(32), master_key=os.urandom(32), user_id=user.pk, version=record.version
    )
    record.save(update_fields=["wrapped_key"])

    report = verify_wrapped_keys(master_key=master_key)

    assert len(report.unreadable) == 1
    assert "search key v1" in report.unreadable[0]


def test_verification_exits_non_zero_when_something_will_not_open(
    master_key: bytes,
) -> None:
    """A verification that reports a problem and succeeds is not a verification."""

    user = make_user(email="verify-fails@example.com")
    provision_user_data_key(user=user, actor=user, master_key=master_key)
    record = UserDataKey.objects.get(user=user)
    record.wrapped_key = wrap_data_key(
        os.urandom(32), master_key=os.urandom(32), user_id=user.pk, version=record.version
    )
    record.save(update_fields=["wrapped_key"])

    with pytest.raises(CommandError, match="Do not rotate or retire"):
        call_command("master_key", "verify")


def test_verification_notes_a_user_with_no_key_without_calling_it_broken(
    master_key: bytes, capsys: Any
) -> None:
    """An account created before provisioning is a gap, not a corruption."""

    make_user(email="verify-keyless@example.com")

    call_command("master_key", "verify")

    output = capsys.readouterr().out
    assert "has no data key yet" in output
    assert "Every wrapped key opened." in output


def test_verification_never_prints_key_material(master_key: bytes, capsys: Any) -> None:
    user = make_user(email="verify-quiet@example.com")
    provision_user_data_key(user=user, actor=user, master_key=master_key)

    call_command("master_key", "verify")

    captured = capsys.readouterr()
    encoded = base64.urlsafe_b64encode(master_key).decode()
    assert encoded not in captured.out + captured.err
    assert master_key.hex() not in captured.out + captured.err
    assert UserDataKey.objects.get(user=user).wrapped_key not in captured.out + captured.err


def test_verification_refuses_when_no_master_key_can_be_found(
    tmp_path: Path, settings: Any, monkeypatch: Any
) -> None:
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(tmp_path / "absent.key")
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    with pytest.raises(CommandError, match="No field-encryption master key"):
        call_command("master_key", "verify")
