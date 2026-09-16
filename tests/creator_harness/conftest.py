"""tests/creator_harness/conftest.py — session-scoped fixture caching.

`08_PRUEBAS_Y_ACEPTACION.md` asks for MP4 fixtures built "vía ffmpeg si
shutil.which —cacheado por sesión— o skip explícito": building one is a real
subprocess call (tens of ms even at 1 second/64px), so it is built ONCE per
pytest session into a session-scoped temp dir and every test that wants a
sample clip gets a path to that same file — never a per-test rebuild, and
never silently skipped without `pytest.skip` naming why.
"""
from __future__ import annotations

import os

import pytest

from tests.creator_harness import fixtures as fx


@pytest.fixture(scope="session")
def harness_cache_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("creator_harness_fixtures", numbered=False)


@pytest.fixture(scope="session")
def sample_mp4_path(harness_cache_dir) -> str:
    """Built once for the whole session. Every test using this fixture must
    treat the file as READ-ONLY input — copy it if a test needs to mutate
    a clip."""
    if not fx.ffmpeg_available():
        pytest.skip("ffmpeg not on PATH; video fixtures unavailable")
    path = os.path.join(str(harness_cache_dir), "sample.mp4")
    if not os.path.exists(path):
        fx.make_mp4(path, seconds=1.0, size="64x64", fps=5)
    return path


@pytest.fixture(scope="session")
def sample_vfr_mp4_path(harness_cache_dir) -> str:
    if not fx.ffmpeg_available():
        pytest.skip("ffmpeg not on PATH; video fixtures unavailable")
    path = os.path.join(str(harness_cache_dir), "sample_vfr.mp4")
    if not os.path.exists(path):
        fx.make_vfr_mp4(path, seconds=1.0, size="64x64")
    return path


@pytest.fixture()
def sample_png_bytes() -> bytes:
    return fx.png_bytes()


@pytest.fixture()
def sample_jpeg_bytes() -> bytes:
    return fx.jpeg_bytes()


@pytest.fixture()
def corrupt_png_bytes_fx() -> bytes:
    return fx.corrupt_png_bytes()


@pytest.fixture()
def sample_wav_bytes() -> bytes:
    return fx.wav_bytes()


@pytest.fixture()
def overlapping_wav_bytes() -> bytes:
    return fx.overlapping_voices_wav_bytes()
