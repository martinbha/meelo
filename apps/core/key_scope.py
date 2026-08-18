"""One unwrap per request, and nothing left behind afterwards.

Unwrapping a data key is an AES-GCM operation against the master key and an
audit write. A page that renders forty encrypted merchant names used to do that
forty times, which is forty rows in the audit log saying the same thing and
forty chances for the plaintext key to be somewhere it should not be.

The obvious repair — cache the key — is the dangerous one. A key in
``django.core.cache`` is a key in Redis; a key on the session is a key in the
database and in a cookie's signed payload; a key on a module global outlives the
request that earned it and is reachable from the next one. So the cache here is
a :class:`~contextvars.ContextVar`, which is exactly as wide as the thing that
needs it: one request, or one worker job, and gone at the end of it either way.

Three properties follow, and each is tested:

**Scoped to a person.** The scope records whose key it holds. A resolver asked
for a different user does not return the cached one — it refuses, because
answering would hand one person's key to code acting for another.

**Cleared on the way out.** The middleware resets the variable in a ``finally``,
so an exception does not leave a key in a worker thread that will serve the next
request. The reference is dropped rather than zeroed: CPython does not let you
overwrite an immutable ``bytes``, and pretending otherwise would be theatre.

**Audited once per scope.** :func:`apps.core.key_management.get_user_data_key`
records an access event on every call. Recording forty per page turns the audit
log into noise that hides the one access that mattered, so the event is written
when the scope opens and not again.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from .errors import ForbiddenError
from .key_management import get_user_data_key, load_master_key


class KeyScopeError(ForbiddenError):
    """A data key was asked for outside the scope entitled to it."""


@dataclass(frozen=True, slots=True)
class DataKeyScope:
    """One user's unwrapped key, for the life of one request or job."""

    user_id: Any
    data_key: bytes
    key_version: int
    #: What opened this scope — ``"request"`` or ``"job"``. Recorded on the
    #: audit event so a key access with no logged-in actor is distinguishable
    #: from one a person caused.
    origin: str

    def matches(self, user: Any) -> bool:
        return self.user_id == getattr(user, "pk", None)


#: Deliberately a ContextVar and not a module global. A global is shared by
#: every request the process serves; this is not.
_scope: ContextVar[DataKeyScope | None] = ContextVar("data_key_scope", default=None)


def current_scope() -> DataKeyScope | None:
    """The scope in force, if there is one."""

    return _scope.get()


def has_scope_for(user: Any) -> bool:
    scope = _scope.get()
    return scope is not None and scope.matches(user)


@contextmanager
def data_key_scope(
    *,
    user: Any,
    actor: Any,
    master_key: bytes | None = None,
    origin: str = "request",
) -> Iterator[DataKeyScope]:
    """Unwrap this user's key once, and clear it on the way out.

    Nesting is allowed and does nothing: an inner scope for the same user
    reuses the outer one rather than unwrapping again, so a service that opens
    a scope defensively does not double the audit trail when its caller already
    did. An inner scope for a *different* user is refused — that is not nesting,
    it is one person's request reaching for another person's key.
    """

    existing = _scope.get()
    if existing is not None:
        if not existing.matches(user):
            raise KeyScopeError("A data-key scope is already open for another user.")
        yield existing
        return

    data_key = get_user_data_key(user=user, actor=actor, master_key=master_key or load_master_key())
    scope = DataKeyScope(
        user_id=user.pk,
        data_key=data_key,
        key_version=user.encryption_key_version,
        origin=origin,
    )
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        # Reset in a finally, so an exception does not leave a key behind in a
        # worker thread that will go on to serve somebody else.
        _scope.reset(token)


def clear_scope() -> None:
    """Drop whatever key this context is holding.

    Called by the middleware at the end of every request and by the worker at
    the end of every job. Dropping the reference rather than overwriting the
    bytes is not laziness: CPython does not let you overwrite an immutable
    ``bytes`` object in place, and code that appeared to do so would be
    reassuring rather than true.
    """

    _scope.set(None)


def resolve_data_key(*, user: Any, actor: Any, master_key: bytes | None = None) -> bytes:
    """This user's key, from the open scope if there is one.

    Falls back to unwrapping when no scope is open, so a management command or
    a test that never opened one still works — it simply pays the unwrap and
    the audit event, which is the honest cost of asking outside a scope.
    """

    scope = _scope.get()
    if scope is not None:
        if not scope.matches(user):
            raise KeyScopeError("The open data-key scope belongs to another user.")
        return scope.data_key
    return get_user_data_key(user=user, actor=actor, master_key=master_key or load_master_key())


def request_data_key(request: Any) -> bytes:
    """The requesting user's key, opening a scope for it on the first ask.

    Views call this instead of unwrapping for themselves. The first call in a
    request pays the unwrap and writes one audit event; every call after it —
    the same view again, a service two layers down, a template helper — gets the
    key already in hand. The middleware clears it when the request ends.

    Opened here rather than in the middleware because most requests decrypt
    nothing, and unwrapping a key for a page that lists dates is work done for
    nobody.
    """

    user = request.user
    scope = _scope.get()
    if scope is not None:
        if not scope.matches(user):
            raise KeyScopeError("The open data-key scope belongs to another user.")
        return scope.data_key
    data_key = get_user_data_key(user=user, actor=user, master_key=load_master_key())
    _scope.set(
        DataKeyScope(
            user_id=user.pk,
            data_key=data_key,
            key_version=user.encryption_key_version,
            origin="request",
        )
    )
    return data_key
