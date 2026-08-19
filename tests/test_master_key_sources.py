"""Where the master key may come from, and what disqualifies it (#165, specification 22.1).

The specification lists several storage options and they are not
interchangeable. Every one ends as the same thirty-two bytes; they differ in how
they fail and how they leak.

The property all three share is that the key is a file only its owner can read.
A key file any account on the host can read is not a secret, and a system that
starts anyway looks encrypted without being it — so the check happens before the
read, and refusing to start is the answer.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

import pytest

from apps.core.key_management import KeyManagementError, load_master_key
from apps.core.master_key import (
    DEFAULT_SECRET_NAME,
    MasterKeySourceError,
    assert_permissions,
    candidate_sources,
    find_source,
)


def write_key(path: Path, *, mode: int = 0o600) -> bytes:
    key = os.urandom(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(base64.urlsafe_b64encode(key).decode(), encoding="ascii")
    path.chmod(mode)
    return key


# ----------------------------------------------------------------------
# The sources
# ----------------------------------------------------------------------


def test_a_configured_path_is_read(tmp_path: Path, settings: Any) -> None:
    path = tmp_path / "master.key"
    key = write_key(path)
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)

    assert load_master_key() == key


def test_a_systemd_credential_is_found_without_configuration(
    tmp_path: Path, settings: Any, monkeypatch: Any
) -> None:
    """A unit run with LoadCredential= needs no path in the environment."""

    key = write_key(tmp_path / DEFAULT_SECRET_NAME)
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = ""
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))

    assert load_master_key() == key


def test_the_docker_secret_mount_is_always_a_candidate(settings: Any, monkeypatch: Any) -> None:
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = ""
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    paths = [str(source.path) for source in candidate_sources()]

    assert paths == [f"/run/secrets/{DEFAULT_SECRET_NAME}"]


def test_a_configured_path_wins_over_the_conventional_ones(
    tmp_path: Path, settings: Any, monkeypatch: Any
) -> None:
    """An operator who named a path meant it; reading a different file is worse than failing."""

    chosen = write_key(tmp_path / "chosen.key")
    write_key(tmp_path / "credentials" / DEFAULT_SECRET_NAME)
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(tmp_path / "chosen.key")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path / "credentials"))

    assert find_source().path == tmp_path / "chosen.key"
    assert load_master_key() == chosen


def test_no_key_anywhere_names_everywhere_that_was_tried(
    tmp_path: Path, settings: Any, monkeypatch: Any
) -> None:
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(tmp_path / "absent.key")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path / "credentials"))

    with pytest.raises(MasterKeySourceError) as failure:
        find_source()

    message = str(failure.value)
    assert "absent.key" in message
    assert "systemd credential" in message
    assert "Docker secret" in message


# ----------------------------------------------------------------------
# Permissions
# ----------------------------------------------------------------------


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o666, 0o755])
def test_a_key_readable_beyond_its_owner_is_refused(
    tmp_path: Path, settings: Any, mode: int
) -> None:
    """Group readable counts: "the group can read it" is a list nobody audits."""

    path = tmp_path / "master.key"
    write_key(path, mode=mode)
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)

    with pytest.raises(KeyManagementError, match="readable beyond its owner"):
        load_master_key()


def test_the_refusal_tells_the_operator_what_to_run(tmp_path: Path) -> None:
    path = tmp_path / "master.key"
    write_key(path, mode=0o644)

    with pytest.raises(MasterKeySourceError, match=f"chmod 600 {path}"):
        assert_permissions(path)


@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_an_owner_only_key_is_accepted(tmp_path: Path, settings: Any, mode: int) -> None:
    path = tmp_path / "master.key"
    key = write_key(path, mode=mode)
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)

    assert load_master_key() == key


def test_permissions_are_checked_before_the_file_is_read(
    tmp_path: Path, settings: Any, monkeypatch: Any
) -> None:
    """A refusal that happens after the secret is in memory has lost the point."""

    path = tmp_path / "master.key"
    write_key(path, mode=0o644)
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)
    reads: list[str] = []

    original = Path.read_text

    def watching(self: Path, *args: Any, **kwargs: Any) -> str:
        reads.append(str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", watching)

    with pytest.raises(KeyManagementError):
        load_master_key()

    assert str(path) not in reads


# ----------------------------------------------------------------------
# The key never appears anywhere it should not
# ----------------------------------------------------------------------


def test_no_failure_message_carries_the_key(tmp_path: Path, settings: Any) -> None:
    path = tmp_path / "master.key"
    key = write_key(path, mode=0o644)
    encoded = base64.urlsafe_b64encode(key).decode()
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)

    with pytest.raises(KeyManagementError) as failure:
        load_master_key()

    message = str(failure.value)
    assert encoded not in message
    assert key.hex() not in message


def test_a_malformed_key_is_reported_without_echoing_its_contents(
    tmp_path: Path, settings: Any
) -> None:
    """A stack trace that helpfully includes the secret is worse than the failure."""

    path = tmp_path / "master.key"
    path.write_text("obviously-not-base64-but-still-secret", encoding="ascii")
    path.chmod(0o600)
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)

    with pytest.raises(KeyManagementError) as failure:
        load_master_key()

    assert "obviously-not-base64" not in str(failure.value)


def test_the_key_is_never_read_from_an_environment_variable(
    tmp_path: Path, settings: Any, monkeypatch: Any
) -> None:
    """An environment variable is readable from /proc and survives into core dumps."""

    key = os.urandom(32)
    monkeypatch.setenv("FIELD_ENCRYPTION_MASTER_KEY", base64.urlsafe_b64encode(key).decode())
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = ""
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    with pytest.raises(KeyManagementError):
        load_master_key()


# ----------------------------------------------------------------------
# Startup
# ----------------------------------------------------------------------


def test_production_refuses_to_start_without_a_valid_key(
    tmp_path: Path, settings: Any, monkeypatch: Any
) -> None:
    """The startup check is what turns a missing key into a failed deploy."""

    from apps.core.apps import CoreConfig

    settings.FIELD_ENCRYPTION_MASTER_KEY_REQUIRED = True
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(tmp_path / "absent.key")
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    with pytest.raises(KeyManagementError):
        CoreConfig.ready(CoreConfig)  # type: ignore[arg-type]


def test_production_starts_when_a_valid_key_is_present(tmp_path: Path, settings: Any) -> None:
    from apps.core.apps import CoreConfig

    path = tmp_path / "master.key"
    write_key(path)
    settings.FIELD_ENCRYPTION_MASTER_KEY_REQUIRED = True
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)

    CoreConfig.ready(CoreConfig)  # type: ignore[arg-type]
