from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django import forms


class ReadableModelChoiceField(forms.ModelChoiceField):  # type: ignore[type-arg]
    """A model choice whose label is built for the current request.

    The builder is installed on the copied per-form field instance, so a key
    or readable value cannot escape into a module-level field or cache.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._label_builder: Callable[[Any], str] | None = None
        super().__init__(*args, **kwargs)

    def set_label_builder(self, builder: Callable[[Any], str]) -> None:
        self._label_builder = builder

    def label_from_instance(self, obj: Any) -> str:
        if self._label_builder is None:
            return "Selection unavailable"
        return self._label_builder(obj)
