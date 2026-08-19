"""Generating and checking the master key, so nobody has to remember an OpenSSL line.

Two operations, and they belong together because they are the two halves of one
question: is there a key, and does it still open what it opened yesterday.

``generate`` writes thirty-two random bytes, base64-encoded, with the mode the
loader requires. It exists because the alternative is a line in a runbook that
somebody retypes at three in the morning — and the ways to get it subtly wrong
are all silent. ``openssl rand -base64 32`` produces a key with a newline and
the standard alphabet rather than the URL-safe one; ``head -c 32 /dev/urandom``
produces raw bytes that are not base64 at all; and every one of them leaves the
file at whatever the umask happened to be.

``verify`` unwraps every stored key with the master key and reports which users
it cannot open. That is the check worth having after a restore, after a key
rotation, and before trusting a backup: a wrapped key that does not unwrap is
not a warning, it is that person's entire financial history gone, and the only
moment it is cheap to discover is before anybody depends on it.

**Nothing here prints key material.** Not the master key, not a data key, not a
wrapped envelope. The generated key goes to the file and nowhere else — not to
standard output, not into a log line, not into an exception. A key that has been
on a terminal is in a scrollback buffer, a shell history, and a screen-sharing
recording.
"""

from __future__ import annotations

import base64
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.core.key_management import (
    KeyManagementError,
    unwrap_data_key,
    unwrap_search_key,
)
from apps.core.master_key import MasterKeySourceError, find_source
from apps.users.models import User, UserDataKey, UserSearchKey

KEY_SIZE = 32
#: What the loader insists on, written here so the file is correct the moment it
#: exists rather than after a separate chmod somebody may not run.
KEY_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR


@dataclass(slots=True)
class VerificationReport:
    """What unwrapped, and what did not."""

    users_checked: int = 0
    keys_checked: int = 0
    #: One line per key that would not open, naming the user and the version.
    #: The user's email is an identifier the operator needs to act; the key
    #: itself never appears.
    unreadable: list[str] = field(default_factory=list)
    users_without_keys: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.unreadable


def generate_master_key(path: Path) -> None:
    """Write a new master key, or refuse because one is already there.

    There is no ``--force``. Overwriting a master key does not replace it, it
    destroys every wrapped key in the database at once and without warning —
    the rows survive and nothing can ever open them again. An operator who
    genuinely wants a new key moves the old one aside deliberately, which is a
    step they will remember taking.
    """

    if path.exists():
        raise CommandError(
            f"{path} already exists. Overwriting a master key makes every "
            "encrypted value in the database permanently unreadable, so this "
            "command will not do it. Move the existing file aside first."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    # Created with the right mode from the start. Writing it first and fixing
    # the permissions afterwards leaves a window in which the key is on disk
    # and world-readable, and that window is all an attacker needs.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, KEY_FILE_MODE)
    try:
        os.write(descriptor, base64.urlsafe_b64encode(os.urandom(KEY_SIZE)))
    finally:
        os.close(descriptor)


def verify_wrapped_keys(*, master_key: bytes) -> VerificationReport:
    """Unwrap every stored key and report the ones that will not open."""

    report = VerificationReport()
    for user in User.objects.all().order_by("email"):
        report.users_checked += 1
        data_keys = list(UserDataKey.objects.filter(user=user).order_by("version"))
        search_keys = list(UserSearchKey.objects.filter(user=user).order_by("version"))
        if not data_keys:
            report.users_without_keys.append(user.email)

        for record in data_keys:
            report.keys_checked += 1
            try:
                unwrap_data_key(
                    record.wrapped_key,
                    master_key=master_key,
                    user_id=user.pk,
                    version=record.version,
                )
            except KeyManagementError as error:
                report.unreadable.append(f"{user.email}: data key v{record.version}: {error}")

        for search_record in search_keys:
            report.keys_checked += 1
            try:
                unwrap_search_key(search_record, master_key=master_key)
            except KeyManagementError as error:
                report.unreadable.append(
                    f"{user.email}: search key v{search_record.version}: {error}"
                )
    return report


class Command(BaseCommand):
    help = "Generate a master key, or verify that every wrapped user key still unwraps."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "action",
            choices=("generate", "verify"),
            help="generate writes a new key; verify checks the stored ones open.",
        )
        parser.add_argument(
            "--path",
            help="Where to write the key. Required for generate.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["action"] == "generate":
            self._generate(options)
        else:
            self._verify()

    def _generate(self, options: dict[str, Any]) -> None:
        if not options.get("path"):
            raise CommandError("generate requires --path.")
        path = Path(str(options["path"])).expanduser()
        generate_master_key(path)
        # The path, the mode, and what to do next. Never the key.
        self.stdout.write(self.style.SUCCESS(f"Wrote a new master key to {path} (mode 0600)."))
        self.stdout.write(
            "Copy it somewhere that is not this machine before putting any data in "
            "the system, and never into the same backup as the database. There is "
            "no recovery path."
        )

    def _verify(self) -> None:
        from apps.core.key_management import load_master_key

        try:
            source = find_source()
            master_key = load_master_key()
        except (MasterKeySourceError, KeyManagementError) as error:
            raise CommandError(str(error)) from error

        report = verify_wrapped_keys(master_key=master_key)
        self.stdout.write(
            f"Read the master key from {source.description} ({source.path}). "
            f"Checked {report.keys_checked} wrapped key(s) across "
            f"{report.users_checked} user(s)."
        )
        for email in report.users_without_keys:
            self.stdout.write(self.style.WARNING(f"  {email} has no data key yet."))
        for problem in report.unreadable:
            self.stderr.write(self.style.ERROR(f"  unreadable {problem}"))
        if not report.is_clean:
            raise CommandError(
                f"{len(report.unreadable)} wrapped key(s) could not be opened with this "
                "master key. Do not rotate or retire anything until this is resolved."
            )
        self.stdout.write(self.style.SUCCESS("Every wrapped key opened."))
