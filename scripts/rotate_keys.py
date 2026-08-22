"""Run the guarded key-rotation sequence from an operator shell.

The backup check is deliberately outside the Django command. It makes the
precondition visible in the same place as the rotation and keeps a typo in a
manual command from turning into an irreversible operation.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_MAX_AGE_HOURS = 24.0
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def backup_age_hours(path: Path, *, now: datetime | None = None) -> float:
    """Return a backup's age, treating a future mtime as zero age."""

    current = now or datetime.now(UTC)
    modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    return max(0.0, (current - modified).total_seconds() / 3600)


def require_recent_backup(path: Path, *, max_age_hours: float, now: datetime | None = None) -> None:
    """Refuse rotation unless the named backup is recent enough to restore."""

    if max_age_hours <= 0:
        raise ValueError("max_age_hours must be greater than zero")
    if not path.is_file():
        raise FileNotFoundError(f"Backup archive does not exist: {path}")
    age = backup_age_hours(path, now=now)
    if age > max_age_hours:
        raise ValueError(
            f"Backup archive is {age:.1f} hours old; the limit is {max_age_hours:.1f} hours."
        )


def python_command(value: str | None) -> list[str]:
    """Parse the configurable command without invoking a shell."""

    command = value or "uv run python"
    parsed = shlex.split(command)
    if not parsed:
        raise ValueError("MEELO_PYTHON_COMMAND cannot be empty")
    return parsed


def build_commands(
    interpreter: Sequence[str],
    *,
    archive: Path,
    email: str | None,
    batch_size: int,
    retire: bool,
) -> list[list[str]]:
    """Build the checks and rotation command in execution order."""

    manage = [*interpreter, "manage.py"]
    rotation = [*manage, "rotate_encryption_keys", "--batch-size", str(batch_size)]
    if email:
        rotation.extend(("--email", email))
    if retire:
        rotation.append("--retire")
    return [
        [*manage, "verify_backup", str(archive)],
        [*manage, "master_key", "verify"],
        rotation,
        [*manage, "master_key", "verify"],
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path, help="Recent encrypted database backup.")
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=DEFAULT_MAX_AGE_HOURS,
        help="Maximum age of the verified backup (default: 24).",
    )
    parser.add_argument("--email", help="Rotate one user only.")
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--retire", action="store_true")
    options = parser.parse_args(argv)

    if not os.environ.get("MEELO_BACKUP_PASSPHRASE"):
        parser.error("MEELO_BACKUP_PASSPHRASE must be set before rotation")
    if options.batch_size < 1:
        parser.error("--batch-size must be at least 1")
    try:
        require_recent_backup(options.backup, max_age_hours=options.max_age_hours)
        commands = build_commands(
            python_command(os.environ.get("MEELO_PYTHON_COMMAND")),
            archive=options.backup,
            email=options.email,
            batch_size=options.batch_size,
            retire=options.retire,
        )
    except (FileNotFoundError, ValueError) as error:
        parser.error(str(error))

    for command in commands:
        subprocess.run(command, cwd=REPOSITORY_ROOT, check=True)
    print("Key rotation completed and verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
