from __future__ import annotations

from typing import Any

from django.http import Http404


def owned_queryset(model: Any, user: Any) -> Any:
    """Return only rows owned by ``user`` for models with a user foreign key."""

    if user is None or not getattr(user, "is_authenticated", False):
        return model.objects.none()
    return model.objects.filter(user_id=user.pk)


def get_owned_object_or_404(model: Any, user: Any, **lookup: Any) -> Any:
    """Resolve an object through its owner scope, never by an unscoped identifier."""

    try:
        return owned_queryset(model, user).get(**lookup)
    except model.DoesNotExist as exc:
        raise Http404 from exc


def assert_owned(obj: Any, user: Any) -> Any:
    """Raise ``Http404`` rather than revealing whether another user's object exists."""

    if user is None or not getattr(user, "is_authenticated", False) or obj.user_id != user.pk:
        raise Http404
    return obj
