"""Reading a row the way a database dump would.

Several tests assert that nothing readable reaches a column. The obvious way to
write that is ``assert secret not in str(instance.__dict__)``, and it is wrong
in a way that hides for months: ``__dict__`` also holds the row's timestamps,
and a timestamp's microseconds are six digits that will eventually contain any
amount you search for. ``342900`` contains ``42900``. The assertion then fails
on a run that changed nothing, on a machine that was merely unlucky.

What the assertion means is "a dump of this row shows nothing readable", and a
dump shows columns. So these helpers render the columns that can hold text —
strings and JSON — and leave the clock out of it.
"""

from __future__ import annotations

from typing import Any

from django.db import models


def stored_text(instance: models.Model) -> str:
    """Every text-bearing column of one row, joined for substring assertions."""

    values: list[str] = []
    for field in instance._meta.concrete_fields:
        value: Any = getattr(instance, field.attname, None)
        if value is None:
            continue
        if isinstance(value, str):
            values.append(value)
        elif isinstance(field, models.JSONField):
            values.append(str(value))
    return "\n".join(values)
