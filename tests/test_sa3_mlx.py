"""SA3MLXBackend against a stub of the upstream runtime (no MLX needed)."""
import sys
import types

import pytest

from audio_studio_mcp.backends import GenerationRequest
from audio_studio_mcp.backends.sa3_mlx import SA3MLXBackend

STUB = '''
import time
from concurrent.futures import ThreadPoolExecutor
import numpy as np
SAMPLE_RATE = 44100
_MLX_EXECUTOR = ThreadPoolExecutor(max_workers=1)
_dit_cache, _dit_lru, _dec_cache, _enc_cache, _cond_cache, _latent_cache = {}, [], {}, {}, {}, {}
_t5 = None
calls, loads = [], []
def _free_to_pool(): pass
def _reconcile_lora(model, dit_name, specs, num_steps): pass
def load_dit(name, T_lat, dtype):
    loads.append((name, T_lat))
    time.sleep(0.002)
    return object(), None
def get_dit(dit_name, T_lat, dtype, lora_specs=None, num_steps=8):
    raise AssertionError("upstream get_dit must be replaced")
def run_generation(dit_name, decoder_name, prompt, negative_prompt, seconds, steps, seed, cfg,
                   apg, sigma_max, a2a_audio_path=None, inpaint_audio_path=None,
                   inpaint_range_sec=None, lora_specs=None):
    print("progress on stdout")
    model, load_ms = get_dit(dit_name, int(seconds * 10), "f16")
    calls.append(dict(dit_name=dit_name, decoder_name=decoder_name, seed=seed, seconds=seconds,
                      sigma_max=sigma_max, a2a=a2a_audio_path, inpaint=inpaint_audio_path,
                      range=inpaint_range_sec))
    return np.zeros((2, int(seconds * SAMPLE_RATE)), np.float32), {"dit_load_ms": load_ms}
'''


@pytest.fixture
def backend(tmp_path, monkeypatch):
    scripts = tmp_path / "stable-audio-3" / "optimized" / "mlx" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "sa3_gradio.py").write_text(STUB)
    mlx = types.ModuleType("mlx")
    core = types.ModuleType("mlx.core")
    core.float16 = "f16"
    core.set_cache_limit = lambda n: setattr(core, "cache_limit", n)
    mlx.core = core
    monkeypatch.setitem(sys.modules, "mlx", mlx)
    monkeypatch.setitem(sys.modules, "mlx.core", core)
    monkeypatch.delitem(sys.modules, "sa3_gradio", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    b = SA3MLXBackend(upstream_dir=tmp_path / "stable-audio-3", model="medium", cache_limit_gb=1)
    yield b
    sys.modules.pop("sa3_gradio", None)


def test_settings(backend):
    assert backend.model_label == "medium + same-l" and backend.max_seconds == 380
    assert backend.sample_rate == 44100
    assert sys.modules["mlx.core"].cache_limit == 2**30


def test_text_to_audio_and_single_dit_instance(backend, capsys):
    g = backend._g
    r1 = backend._generate([5, 6], GenerationRequest(prompt="p", seconds=2))
    r2 = backend._generate([7], GenerationRequest(prompt="p", seconds=9))
    assert [c["seed"] for c in g.calls] == [5, 6, 7]
    assert g.calls[0]["sigma_max"] == 1.0 and g.calls[0]["a2a"] is None
    assert len(g.loads) == 1, "one DiT instance must serve every length"
    assert r1.included_model_load and not r2.included_model_load
    assert r1.audios[0].shape == (2, 88200)
    assert "progress on stdout" not in capsys.readouterr().out


def test_edit_requests_map_to_upstream_kwargs(backend):
    g = backend._g
    backend._generate([1], GenerationRequest(prompt="p", seconds=4, init_audio="a.wav", init_strength=0.4))
    assert g.calls[-1]["a2a"] == "a.wav" and g.calls[-1]["sigma_max"] == 0.4
    backend._generate([1], GenerationRequest(prompt="p", seconds=6, inpaint_audio="b.wav", inpaint_range=(4, 6)))
    assert g.calls[-1]["inpaint"] == "b.wav" and g.calls[-1]["range"] == (4, 6)
    assert g.calls[-1]["sigma_max"] == 1.0


def test_unload(backend):
    backend._generate([1], GenerationRequest(prompt="p", seconds=1))
    assert backend.loaded()
    assert backend._unload() is True and not backend.loaded()
    assert backend._unload() is False


def test_bad_dir_and_model(tmp_path):
    with pytest.raises(RuntimeError, match="install.sh"):
        SA3MLXBackend(upstream_dir=tmp_path / "missing")
