"""Where the master key comes from, and what makes a source acceptable.

Specification 22.1 lists several places the master key may live. They are not
interchangeable, and the differences are operational rather than cryptographic —
every one of them ends up as the same thirty-two bytes, but they fail in
different ways and leak in different ways.

**A Docker secret** (``/run/secrets/<name>``) is a tmpfs file the daemon mounts
into the container. It is not in the image, not in the Compose file, and not in
the container's environment, so ``docker inspect`` does not show it. It is the
default here.

**A systemd credential** (``$CREDENTIALS_DIRECTORY/<name>``) is the same idea
without Docker: systemd places the file in a per-unit directory readable only by
the service user, and removes it when the unit stops. Preferred when the
application runs as a unit rather than in a container.

**A root-owned environment file** is the weakest of the three the specification
allows, and it is allowed because sometimes it is what an operator has. An
environment variable is visible to anything that can read ``/proc/<pid>/environ``,
survives into core dumps, and is inherited by every child process — so this
loader reads the *file*, never an environment variable holding the key itself.

What all three have in common is that the key is a file with restrictive
permissions, and that is what is checked. A key file any user on the host can
read is not a secret; it is a file. Refusing to start is the correct response,
because the alternative is a system that looks encrypted and is not.

The key's *bytes* never reach a log line, an error message, or an exception —
every failure here names the path and the problem, never the contents. A stack
trace that helpfully includes the secret is a worse outcome than the failure it
was reporting.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

#: Permission bits that must not be set on a key file. Group and world read are
#: both refused: "the group can read it" is a list of accounts nobody audits.
FORBIDDEN_MODE_BITS = stat.S_IRWXG | stat.S_IRWXO

#: The conventional Docker secret mount point.
DOCKER_SECRETS_DIRECTORY = "/run/secrets"

#: Default file name under a secret or credential directory.
DEFAULT_SECRET_NAME = "field_encryption_master_key"


class MasterKeySourceError(RuntimeError):
    """The master key cannot be loaded from any configured source."""


@dataclass(frozen=True, slots=True)
class KeySource:
    """One candidate location, and how it was arrived at."""

    path: Path
    #: For the operator error: "tried the Docker secret at ..." is actionable,
    #: "tried /run/secrets/x" alone is a guess about what they configured.
    description: str


def _configured_path() -> Path | None:
    configured = str(getattr(settings, "FIELD_ENCRYPTION_MASTER_KEY_FILE", "") or "").strip()
    if not configured or configured == ".":
        return None
    return Path(configured)


def candidate_sources() -> tuple[KeySource, ...]:
    """Every place to look, in the order they are tried.

    An explicit setting wins, because an operator who named a path meant it and
    silently reading a different file would be worse than failing. The
    conventional locations follow, so a standard Compose or systemd deployment
    needs no configuration at all.
    """

    sources: list[KeySource] = []
    configured = _configured_path()
    if configured is not None:
        sources.append(KeySource(configured, "the configured FIELD_ENCRYPTION_MASTER_KEY_FILE"))

    credentials = os.environ.get("CREDENTIALS_DIRECTORY", "").strip()
    if credentials:
        sources.append(
            KeySource(
                Path(credentials) / DEFAULT_SECRET_NAME,
                "the systemd credential directory",
            )
        )

    sources.append(
        KeySource(
            Path(DOCKER_SECRETS_DIRECTORY) / DEFAULT_SECRET_NAME,
            "the Docker secret mount",
        )
    )
    return tuple(sources)


def assert_permissions(path: Path) -> None:
    """Refuse a key file anybody but its owner can read.

    Checked before reading rather than after, so a world-readable key is never
    loaded into the process at all — a failure that happens after the secret is
    in memory has already lost most of what refusing was for.
    """

    try:
        info = path.stat()
    except OSError as exc:
        raise MasterKeySourceError(f"The master key at {path} cannot be inspected.") from exc

    mode = stat.S_IMODE(info.st_mode)
    if mode & FORBIDDEN_MODE_BITS:
        raise MasterKeySourceError(
            f"The master key at {path} is readable beyond its owner "
            f"(mode {mode:04o}). Run: chmod 600 {path}"
        )


def read_source(source: KeySource) -> str:
    """The encoded key from one source, permissions checked first."""

    assert_permissions(source.path)
    try:
        return source.path.read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        # The path and the reason, never the contents. A file that failed to
        # decode as ASCII must not have its bytes echoed into the message.
        raise MasterKeySourceError(
            f"The master key at {source.path} cannot be read: {type(exc).__name__}."
        ) from exc


def available_sources() -> Iterator[KeySource]:
    for source in candidate_sources():
        if source.path.is_file():
            yield source


def find_source() -> KeySource:
    """The first source that exists, or an error naming everywhere that was tried."""

    for source in available_sources():
        return source
    tried = ", ".join(f"{source.description} ({source.path})" for source in candidate_sources())
    raise MasterKeySourceError(
        "No field-encryption master key was found. Tried: " + (tried or "nothing configured") + "."
    )
