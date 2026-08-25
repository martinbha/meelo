from typing import Any
from unittest.mock import patch

from apps.reconciliation.apps import ReconciliationConfig


def test_automatic_merge_policy_is_disabled_by_default(settings: Any) -> None:
    assert settings.AUTOMATIC_RECONCILIATION_MERGE_ENABLED is False


def test_startup_logs_the_automatic_merge_policy(
    settings: Any,
) -> None:
    settings.AUTOMATIC_RECONCILIATION_MERGE_ENABLED = True

    with patch("apps.reconciliation.apps.logger.info") as info:
        ReconciliationConfig.ready(ReconciliationConfig)  # type: ignore[arg-type]

    info.assert_called_once_with(
        "Automatic reconciliation merge policy enabled=%s",
        True,
    )
