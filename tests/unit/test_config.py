"""Unit tests for Settings.apply_live validation."""

from __future__ import annotations

import pytest

from lancast.config import Settings


def test_valid_patch_applies_and_returns_applied_keys():
    settings = Settings()
    applied = settings.apply_live(
        {"fps": 30, "max_width": 1280, "approval": False, "jpeg_quality": 80.0}
    )
    assert applied == {"fps": 30, "max_width": 1280, "approval": False, "jpeg_quality": 80}
    assert settings.fps == 30
    assert settings.max_width == 1280
    assert settings.approval is False
    assert settings.jpeg_quality == 80
    assert isinstance(settings.jpeg_quality, int)


def test_boundary_values_are_accepted():
    settings = Settings()
    assert settings.apply_live(
        {
            "fps": 1,
            "max_width": 320,
            "max_height": 240,
            "bitrate_kbps": 500,
            "jpeg_quality": 20,
            "jpeg_fps": 1,
        }
    ) == {
        "fps": 1,
        "max_width": 320,
        "max_height": 240,
        "bitrate_kbps": 500,
        "jpeg_quality": 20,
        "jpeg_fps": 1,
    }

    settings = Settings()
    assert settings.apply_live(
        {
            "fps": 120,
            "max_width": 7680,
            "max_height": 4320,
            "bitrate_kbps": 100_000,
            "jpeg_quality": 95,
            "jpeg_fps": 60,
        }
    ) == {
        "fps": 120,
        "max_width": 7680,
        "max_height": 4320,
        "bitrate_kbps": 100_000,
        "jpeg_quality": 95,
        "jpeg_fps": 60,
    }


def test_integral_float_is_accepted_as_integer():
    settings = Settings()
    assert settings.apply_live({"fps": 2.0}) == {"fps": 2}
    assert settings.fps == 2
    assert isinstance(settings.fps, int)


@pytest.mark.parametrize(
    "patch",
    [
        {"nope": 1},
        {"max_viewers": 4},
        {"bind": "127.0.0.1"},
        {"port": 9000},
        {"data_dir": "/tmp/x"},
        {"fps": "30"},
        {"fps": True},
        {"fps": 30.5},
        {"fps": 0},
        {"fps": 121},
        {"max_width": 319},
        {"max_width": 7681},
        {"max_height": 239},
        {"max_height": 4321},
        {"bitrate_kbps": 499},
        {"bitrate_kbps": 100_001},
        {"jpeg_quality": 19},
        {"jpeg_quality": 96},
        {"jpeg_fps": 0},
        {"jpeg_fps": 61},
        {"approval": 1},
        {"approval": "true"},
        {"approval": 0.0},
    ],
)
def test_invalid_patch_raises_and_changes_nothing(patch):
    settings = Settings()
    before = settings.public_dict()
    with pytest.raises(ValueError):
        settings.apply_live(patch)
    assert settings.public_dict() == before


@pytest.mark.parametrize(
    "patch",
    [
        {"fps": 30, "max_viewers": 9},
        {"max_viewers": 9, "fps": 30},
        {"fps": 30, "jpeg_quality": 999},
        {"nope": 1, "fps": 30},
    ],
)
def test_partially_invalid_patch_is_rejected_atomically(patch):
    settings = Settings()
    before = settings.public_dict()
    with pytest.raises(ValueError):
        settings.apply_live(patch)
    assert settings.public_dict() == before
    assert settings.fps == 60
    assert settings.jpeg_quality == 70


def test_non_dict_patch_is_rejected():
    with pytest.raises(ValueError):
        Settings().apply_live([("fps", 30)])
