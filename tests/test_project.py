import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_project_metadata_declares_supported_python() -> None:
    metadata = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())

    assert metadata["project"]["requires-python"] == ">=3.12"
    assert (PROJECT_ROOT / "uv.lock").is_file()
