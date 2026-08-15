from pathlib import Path

import pytest
from PIL import Image

from apps.ocr.contracts import OcrConfigurationError
from apps.ocr.preprocessing import PreprocessingSettings, preprocess_image


def test_preprocessing_corrects_exif_and_generates_deterministic_variants(tmp_path: Path) -> None:
    source = tmp_path / "rotated.jpg"
    image = Image.new("RGB", (20, 10), "gray")
    exif = image.getexif()
    exif[274] = 6
    image.save(source, exif=exif)
    settings = PreprocessingSettings(scale=2, threshold=128)

    first_hashes: list[str]
    with preprocess_image(source, tmp_path / "work", settings) as result:
        assert [item.name for item in result.variants] == [
            "normalized",
            "grayscale",
            "threshold",
        ]
        assert (result.variants[0].width, result.variants[0].height) == (20, 40)
        assert result.selected_variant == "threshold"
        assert result.settings.serializable()["scale"] == 2
        first_hashes = [item.sha256 for item in result.variants]
        paths = [item.path for item in result.variants]
        assert all(path.exists() for path in paths)
    assert all(not path.exists() for path in paths)

    with preprocess_image(source, tmp_path / "work", settings) as repeated:
        assert [item.sha256 for item in repeated.variants] == first_hashes


def test_preprocessing_applies_crop_and_rotation(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    Image.new("RGB", (100, 60), "white").save(source)
    settings = PreprocessingSettings(
        crop=(10, 10, 50, 30),
        rotation=90,
        include_grayscale=False,
        include_threshold=False,
    )

    with preprocess_image(source, tmp_path / "work", settings) as result:
        assert (result.variants[0].width, result.variants[0].height) == (20, 40)
        assert result.selected_variant == "normalized"


def test_preprocessing_cleans_variants_when_pipeline_fails(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    Image.new("RGB", (20, 20), "white").save(source)
    paths: list[Path] = []

    with (
        pytest.raises(RuntimeError, match="pipeline"),
        preprocess_image(source, tmp_path / "work", PreprocessingSettings()) as result,
    ):
        paths = [item.path for item in result.variants]
        raise RuntimeError("pipeline failed")
    assert paths
    assert all(not path.exists() for path in paths)


def test_preprocessing_rejects_invalid_crop_and_missing_source(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="crop"):
        PreprocessingSettings(crop=(5, 5, 4, 8))
    with pytest.raises(OcrConfigurationError, match="does not exist"), preprocess_image(
        tmp_path / "missing.png", tmp_path / "work", PreprocessingSettings()
    ):
        pass
