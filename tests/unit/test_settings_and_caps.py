"""Explicit CLI values vs saved admin settings, per-key settings load, viewer quality caps."""

from __future__ import annotations

import json

from lancast.cli import build_parser, settings_from_args
from lancast.config import Settings
from lancast.pipeline import quality_key


def _settings(tmp_path, *cli):
    args = build_parser().parse_args(["serve", "--data-dir", str(tmp_path), *cli])
    return settings_from_args(args)


def test_saved_settings_apply_when_not_given_on_command_line(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"fps": 30, "bitrate_kbps": 8000}), encoding="utf-8")
    s = _settings(tmp_path)
    assert (s.fps, s.bitrate_kbps) == (30, 8000)


def test_explicit_cli_value_wins_even_when_equal_to_default(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"fps": 30, "max_width": 1280, "max_height": 720,
                                                        "bitrate_kbps": 8000}), encoding="utf-8")
    s = _settings(tmp_path, "--fps", "60", "--max-res", "1080p", "--bitrate", "10M")
    assert (s.fps, s.max_width, s.max_height, s.bitrate_kbps) == (60, 1920, 1080, 10_000)


def test_defaults_without_saved_settings(tmp_path):
    s = _settings(tmp_path)
    assert (s.fps, s.max_width, s.max_height, s.bitrate_kbps, s.jpeg_quality, s.jpeg_fps) == \
        (60, 1920, 1080, 10_000, 70, 15)


def test_one_bad_saved_value_does_not_discard_the_rest(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({
        "fps": 30, "bitrate_kbps": 10**9, "jpeg_quality": "high", "approval": False, "unknown": 1,
    }), encoding="utf-8")
    s = Settings(data_dir=tmp_path)
    applied = s.load_live()
    assert applied == {"fps": 30, "approval": False}
    assert s.bitrate_kbps == 10_000 and s.jpeg_quality == 70


def _custom(**kw):
    return {"preset": "custom", "max_width": 1920, "max_height": 1080, **kw}


def test_viewer_custom_quality_is_capped_by_owner_settings():
    s = Settings()                                     # fps 60, 10 Mbps
    assert quality_key(_custom(fps=240, bitrate_kbps=200_000), "h264", s) == ("custom", 1920, 1080, 60, 35_000)
    assert quality_key(_custom(fps=30, bitrate_kbps=5_000), "h264", s) == ("custom", 1920, 1080, 30, 5_000)
    s.fps, s.bitrate_kbps = 240, 120_000               # the owner raises the limits
    assert quality_key(_custom(fps=240, bitrate_kbps=200_000), "h264", s) == ("custom", 1920, 1080, 240, 120_000)


def test_viewer_custom_jpeg_fps_is_capped():
    s = Settings()                                     # jpeg_fps 15
    assert quality_key(_custom(fps=240, jpeg_quality=90), "jpeg", s) == ("custom", 1920, 1080, 30, 90)
