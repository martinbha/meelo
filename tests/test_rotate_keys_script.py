"""Tests for the operator-side key rotation guard."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.rotate_keys import backup_age_hours, build_commands, require_recent_backup


def test_recent_backup_is_accepted(tmp_path: Path) -> None:
    archive = tmp_path / "backup.enc"
    archive.write_bytes(b"sealed")
    now = datetime.now(UTC)

    require_recent_backup(archive, max_age_hours=24, now=now)
    assert backup_age_hours(archive, now=now) >= 0


def test_old_backup_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "backup.enc"
    archive.write_bytes(b"sealed")
    now = datetime.now(UTC)

    with pytest.raises(ValueError, match="hours old"):
        require_recent_backup(archive, max_age_hours=1, now=now + timedelta(hours=2))


def test_rotation_commands_verify_before_and_after() -> None:
    commands = build_commands(
        ["uv", "run", "python"],
        archive=Path("/secure/backup.enc"),
        email="owner@example.com",
        batch_size=50,
        retire=True,
    )

    assert [command[4:] for command in commands] == [
        ["verify_backup", "/secure/backup.enc"],
        ["master_key", "verify"],
        [
            "rotate_encryption_keys",
            "--batch-size",
            "50",
            "--email",
            "owner@example.com",
            "--retire",
        ],
        ["master_key", "verify"],
    ]
