import shutil

import numpy as np
import pytest

from audio_studio_mcp import post
from conftest import sine

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def test_peak_ceiling_only_turns_down():
    quiet = sine(amp=0.1)
    out, gain = post.peak_ceiling(quiet, -1.0)
    assert gain == 0.0 and out is quiet
    loud = sine(amp=1.5)
    out, gain = post.peak_ceiling(loud, -1.0)
    assert gain < 0
    assert post.stats(out)["peak_dbfs"] == pytest.approx(-1.0, abs=0.01)


def test_peak_normalize_boosts():
    out, gain = post.peak_normalize(sine(amp=0.1), -1.0)
    assert gain > 0
    assert post.stats(out)["peak_dbfs"] == pytest.approx(-1.0, abs=0.01)


def test_trim():
    a = sine(2.0, sr=1000)
    assert post.trim(a, 1000, 0.5, 1.5).shape == (2, 1000)
    with pytest.raises(ValueError):
        post.trim(a, 1000, 1.5, 1.0)


def test_trim_silence():
    sr = 44100
    a = np.concatenate([np.zeros((2, sr)), sine(1.0, sr), np.zeros((2, sr))], axis=1).astype(np.float32)
    out = post.trim_silence(a, sr, -50)
    assert 1.0 <= out.shape[-1] / sr < 1.1


def test_fade_edges_are_silent():
    out = post.fade(sine(1.0), 44100, 100, 100)
    assert abs(out[:, 0]).max() == 0 and abs(out[:, -1]).max() < 1e-3


def test_make_loop_is_continuous():
    sr = 1000
    ramp = np.tile(np.linspace(0, 1, 3 * sr, dtype=np.float32), (2, 1))
    out = post.make_loop(ramp, sr, 500)
    assert out.shape[-1] == 3 * sr - 500
    # the last sample flows into the first: the start equals the original tail
    assert out[0, 0] == pytest.approx(ramp[0, -500], abs=1e-3)
    with pytest.raises(ValueError):
        post.make_loop(ramp, sr, 2000)


def test_to_mono():
    assert post.to_mono(sine()).shape[0] == 1


@pytest.mark.parametrize("fmt,ext", [("wav", ".wav"), ("flac", ".flac"), ("ogg", ".ogg")])
def test_write_read_roundtrip(tmp_path, fmt, ext):
    path = tmp_path / f"x{ext}"
    post.write(path, sine(0.5), 44100, fmt)
    audio, sr = post.read(path)
    assert sr == 44100 and audio.shape[0] == 2
    assert abs(audio.shape[-1] - 22050) < 2000  # vorbis pads a little


@needs_ffmpeg
def test_ffmpeg_resample():
    out, info = post.ffmpeg_process(sine(1.0), 44100, 48000, None, -1.0)
    assert out.shape[-1] == pytest.approx(48000, abs=10)
    assert info == {}


@needs_ffmpeg
def test_ffmpeg_loudness():
    out, info = post.ffmpeg_process(sine(5.0, amp=0.05), 44100, 48000, -16.0, -1.0)
    assert info["output_lufs"] == pytest.approx(-16.0, abs=1.0)


def test_audible_end():
    sr = 1000
    a = np.concatenate([sine(2.0, sr=sr, freq=50), np.zeros((2, sr), np.float32)], axis=1)
    assert post.audible_end(a, sr) == pytest.approx(2.0, abs=0.01)
    assert post.audible_end(np.zeros((2, sr), np.float32), sr) == 0.0
