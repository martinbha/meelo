from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from .tesseract import SUPPORTED_PSM_MODES

MEASUREMENTS_PATH = Path(__file__).with_name("data") / "tesseract_psm_accuracy.json"
MEASURED_MODES = frozenset({6, 11, 12})


@lru_cache(maxsize=1)
def load_psm_measurements() -> dict[str, dict[int, float]]:
    """Load the checked-in fixture measurements that define source defaults."""

    payload: dict[str, Any] = json.loads(MEASUREMENTS_PATH.read_text(encoding="utf-8"))
    measurements: dict[str, dict[int, float]] = {}
    for source_type, raw_scores in payload["source_types"].items():
        scores = {int(mode): float(score) for mode, score in raw_scores.items()}
        if set(scores) != MEASURED_MODES:
            raise ValueError(f"PSM measurements are incomplete for {source_type}.")
        if any(mode not in SUPPORTED_PSM_MODES for mode in scores):
            raise ValueError(f"PSM measurements contain an unsupported mode for {source_type}.")
        if any(not 0.0 <= score <= 1.0 for score in scores.values()):
            raise ValueError(f"PSM accuracy is outside the valid range for {source_type}.")
        measurements[str(source_type)] = scores
    return measurements


def default_psm(source_type: str) -> int:
    """Select the best measured mode, preferring the lower mode on a tie."""

    measurements = load_psm_measurements()
    scores = measurements.get(source_type, measurements["unknown"])
    return min(scores, key=lambda mode: (-scores[mode], mode))
