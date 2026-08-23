from __future__ import annotations

from pathlib import Path

from scripts.check_logging import check_paths


def test_logging_check_rejects_sensitive_message_and_argument(tmp_path: Path) -> None:
    source = tmp_path / "unsafe.py"
    source.write_text('logger.info("merchant=%s", observation.merchant)\n', encoding="utf-8")

    violations = check_paths((source,))

    assert len(violations) == 1
    assert "sensitive field" in violations[0].reason


def test_logging_check_rejects_raw_exception_arguments(tmp_path: Path) -> None:
    source = tmp_path / "unsafe.py"
    source.write_text('logger.warning("failed: %s", exc)\n', encoding="utf-8")

    violations = check_paths((source,))

    assert len(violations) == 1
    assert "exc" in violations[0].reason


def test_repository_logging_sources_pass_the_check() -> None:
    root = Path(__file__).resolve().parents[1]

    assert check_paths(root / name for name in ("apps", "config", "scripts")) == ()
