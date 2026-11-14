"""Unit tests for the startup encoder benchmark/cache."""

from __future__ import annotations

from types import SimpleNamespace

from lancast.config import Settings
from lancast.encode import autotune


DISPLAY = SimpleNamespace(width=2880, height=1800)


def _results():
    return [
        {"backend": "libx264", "median_ms": 4.5, "fps": 222.0, "error": None},
        {"backend": "h264_qsv", "median_ms": 12.0, "fps": 83.3, "error": None},
    ]


def test_choose_encoder_benchmarks_and_caches(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    calls = {"n": 0}

    def fake_bench(*args, **kwargs):
        calls["n"] += 1
        return _results()

    monkeypatch.setattr(autotune, "probe_h264_encoders", lambda: ["libx264", "h264_qsv"])
    monkeypatch.setattr(autotune, "benchmark_backends", fake_bench)

    chosen, report = autotune.choose_encoder(settings, DISPLAY, ["libx264", "h264_qsv"])
    assert chosen == "libx264"
    assert report["chosen"] == "libx264"
    assert calls["n"] == 1
    assert (tmp_path / autotune.CACHE_FILE).exists()

    # A second start must reuse the cache without touching the benchmark.
    def boom(*args, **kwargs):
        raise AssertionError("benchmark should not run when the cache is valid")

    monkeypatch.setattr(autotune, "benchmark_backends", boom)
    settings.encoder = "auto"
    chosen2, _ = autotune.choose_encoder(settings, DISPLAY, ["libx264", "h264_qsv"])
    assert chosen2 == "libx264"


def test_explicit_encoder_skips_benchmark(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, encoder="h264_qsv")
    monkeypatch.setattr(
        autotune, "benchmark_backends",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not benchmark")),
    )
    assert autotune.choose_encoder(settings, DISPLAY, ["libx264", "h264_qsv"])[0] == "h264_qsv"


def test_cache_is_invalidated_when_output_size_changes(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    monkeypatch.setattr(autotune, "probe_h264_encoders", lambda: ["libx264", "h264_qsv"])
    monkeypatch.setattr(autotune, "benchmark_backends", lambda *a, **k: _results())
    autotune.choose_encoder(settings, DISPLAY, ["libx264", "h264_qsv"])

    calls = {"n": 0}

    def counting(*args, **kwargs):
        calls["n"] += 1
        return _results()

    monkeypatch.setattr(autotune, "benchmark_backends", counting)
    settings.encoder = "auto"
    settings.max_width = 1280
    settings.max_height = 720
    autotune.choose_encoder(settings, DISPLAY, ["libx264", "h264_qsv"])
    assert calls["n"] == 1
