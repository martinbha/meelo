from importlib import import_module

import pytest


@pytest.mark.parametrize(
    ("module", "debug"),
    [("config.settings.development", True), ("config.settings.testing", False)],
)
def test_non_production_settings_are_explicit(module: str, debug: bool) -> None:
    settings_module = import_module(module)

    assert settings_module.DEBUG is debug
