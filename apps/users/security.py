"""What the account security page knows, assembled without knowing anything else.

The page answers four questions a person actually asks: when did I last change
my password, is two-factor on, what else is signed in as me, and has anything
happened to this account that I did not do.

Everything here is deliberately narrow. It reads the audit log, the session
table, and the device tables — and it never touches a financial model. That is
not tidiness: a security page that renders an amount is a page that leaks one to
anybody looking over a shoulder while somebody checks their sessions, and it
would do so on the one page a person opens *because* they are worried.

Audit events are shown as **codes**, not as their metadata. An event's metadata
carries identifiers and counts that were safe to store because nothing renders
them; rendering them here would make that assumption false everywhere at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.contrib.sessions.models import Session
from django.utils import timezone

from apps.core.models import AuditEvent

#: Events worth showing on a security page. Financial workflow events are not
#: here: this page is about the account, and a list of everything the person did
#: last week is a different page with different privacy properties.
SECURITY_EVENT_TYPES: tuple[str, ...] = (
    AuditEvent.EventType.LOGIN_SUCCESS,
    AuditEvent.EventType.LOGIN_FAILURE,
    AuditEvent.EventType.LOGOUT,
    AuditEvent.EventType.PASSWORD_CHANGED,
    AuditEvent.EventType.TWO_FACTOR_ENABLED,
    AuditEvent.EventType.TWO_FACTOR_DISABLED,
    AuditEvent.EventType.TWO_FACTOR_FAILURE,
    AuditEvent.EventType.ENCRYPTION_KEY_ROTATED,
    AuditEvent.EventType.SEARCH_KEY_ROTATED,
    AuditEvent.EventType.WORKER_KEY_ACCESSED,
)

#: How many recent events to show. Enough to spot something unexpected, few
#: enough that a person reads them rather than scrolling past.
RECENT_EVENT_LIMIT = 20

#: A password older than this is worth mentioning. Not enforced — an expiry
#: policy makes people choose worse passwords and write them down — but a date
#: somebody has not thought about in two years is worth putting in front of them.
PASSWORD_AGE_NOTICE = timedelta(days=365)


@dataclass(frozen=True, slots=True)
class SecurityEvent:
    """One audit entry, reduced to what is safe to render."""

    #: The event type. A code, deliberately: the metadata behind it holds
    #: identifiers and counts that were only ever meant for an operator reading
    #: the log, not for a page.
    code: str
    label: str
    occurred_at: datetime
    #: Whether this is the kind of entry a person should look twice at.
    is_notable: bool


@dataclass(frozen=True, slots=True)
class ActiveSession:
    """One signed-in session, described without its contents."""

    key_prefix: str
    expires_at: datetime
    is_current: bool

    @property
    def label(self) -> str:
        return "This browser" if self.is_current else f"Session {self.key_prefix}"


@dataclass(frozen=True, slots=True)
class SecurityOverview:
    """Everything the page renders."""

    email: str
    password_changed_at: datetime | None
    password_age_days: int | None
    two_factor_enabled: bool
    confirmed_device_count: int
    recovery_codes_remaining: int
    sessions: tuple[ActiveSession, ...]
    recent_events: tuple[SecurityEvent, ...]
    last_login_at: datetime | None
    failed_logins_recently: int

    @property
    def password_is_old(self) -> bool:
        return self.password_age_days is not None and (
            self.password_age_days >= PASSWORD_AGE_NOTICE.days
        )

    @property
    def other_session_count(self) -> int:
        return sum(1 for session in self.sessions if not session.is_current)


def _password_changed_at(user: Any) -> datetime | None:
    """When the password last changed, from the audit log.

    Django does not record it. The audit log does, which is the only place it
    exists — and if there is no such event the account has had one password
    since it was created, so the join date is the honest answer.
    """

    event = (
        AuditEvent.objects.filter(user=user, event_type=AuditEvent.EventType.PASSWORD_CHANGED)
        .order_by("-created_at")
        .first()
    )
    if event is not None:
        return event.created_at
    return user.date_joined


def _sessions_for(user: Any, *, current_key: str = "") -> tuple[ActiveSession, ...]:
    """Unexpired sessions belonging to this user.

    Every session in the table has to be decoded to find its owner, because the
    user identifier lives inside the signed payload rather than in a column.
    That is fine at this scale and would not be at another; the alternative is a
    table of our own that can disagree with the real one.

    Only a **prefix** of the key is shown. The full key is a bearer credential:
    anybody who reads it off the screen is signed in as this person.
    """

    now = timezone.now()
    found = []
    for session in Session.objects.filter(expire_date__gt=now):
        try:
            data = session.get_decoded()
        except Exception:  # noqa: BLE001 - a corrupt session is not this page's problem
            continue
        if str(data.get("_auth_user_id", "")) != str(user.pk):
            continue
        found.append(
            ActiveSession(
                key_prefix=session.session_key[:8],
                expires_at=session.expire_date,
                is_current=session.session_key == current_key,
            )
        )
    return tuple(sorted(found, key=lambda item: (not item.is_current, item.expires_at)))


def _recent_events(user: Any) -> tuple[SecurityEvent, ...]:
    labels = dict(AuditEvent.EventType.choices)
    notable = {
        AuditEvent.EventType.LOGIN_FAILURE,
        AuditEvent.EventType.PASSWORD_CHANGED,
        AuditEvent.EventType.ENCRYPTION_KEY_ROTATED,
        AuditEvent.EventType.SEARCH_KEY_ROTATED,
    }
    return tuple(
        SecurityEvent(
            code=event.event_type,
            label=str(labels.get(event.event_type, event.event_type)),
            occurred_at=event.created_at,
            is_notable=event.event_type in notable,
        )
        for event in AuditEvent.objects.filter(
            user=user, event_type__in=SECURITY_EVENT_TYPES
        ).order_by("-created_at")[:RECENT_EVENT_LIMIT]
    )


def security_overview(user: Any, *, current_session_key: str = "") -> SecurityOverview:
    """Assemble the page's facts for one user, and only for that user."""

    from django_otp.plugins.otp_static.models import StaticToken
    from django_otp.plugins.otp_totp.models import TOTPDevice

    from .models import RecoveryCode

    changed_at = _password_changed_at(user)
    devices = TOTPDevice.objects.filter(user=user, confirmed=True).count()
    recovery = RecoveryCode.objects.filter(user=user, used_at__isnull=True).count()
    recovery += StaticToken.objects.filter(device__user=user).count()
    failures = AuditEvent.objects.filter(
        user=user,
        event_type=AuditEvent.EventType.LOGIN_FAILURE,
        created_at__gte=timezone.now() - timedelta(days=30),
    ).count()
    return SecurityOverview(
        email=user.email,
        password_changed_at=changed_at,
        password_age_days=(timezone.now() - changed_at).days if changed_at else None,
        two_factor_enabled=devices > 0,
        confirmed_device_count=devices,
        recovery_codes_remaining=recovery,
        sessions=_sessions_for(user, current_key=current_session_key),
        recent_events=_recent_events(user),
        last_login_at=user.last_login,
        failed_logins_recently=failures,
    )
