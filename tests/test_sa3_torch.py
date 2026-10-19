"""SA3TorchBackend against stubs of torch and the upstream runtime (no GPU needed)."""
import sys
import types

import numpy as np
import pytest
import soundfile as sf

from audio_studio_mcp.backends import GenerationRequest, resolve_backend_name
from audio_studio_mcp.backends.sa3_torch import SA3TorchBackend

UPSTREAM_MODEL = '''
import numpy as np
calls, loads = [], []

class _T:
    def __init__(self, a): self.a = a
    def __getitem__(self, i): return _T(self.a[i])
    def float(self): return self
    def cpu(self): return self
    def numpy(self): return self.a

class _Inner:
    sample_rate = 44100

class StableAudioModel:
    def __init__(self):
        self.model = _Inner()
        self.model_config = {"sample_size": 16777216}

    @staticmethod
    def from_pretrained(model_name, device=None):
        print("loading on stdout")
        loads.append((model_name, device))
        return StableAudioModel()

    def generate(self, prompt=None, duration=120, seed=-1, **kw):
        print("progress on stdout")
        calls.append(dict(prompt=prompt, duration=duration, seed=seed, **kw))
        return _T(np.zeros((1, 2, int(duration * 44100)), np.float32))
'''


@pytest.fixture
def upstream(tmp_path, monkeypatch):
    pkg = tmp_path / "stable-audio-3" / "stable_audio_3"
    (pkg / "models").mkdir(parents=True)
    (pkg / "__init__.py").write_text("from stable_audio_3.model import StableAudioModel\n")
    (pkg / "model.py").write_text(UPSTREAM_MODEL)
    (pkg / "models" / "__init__.py").write_text("")
    (pkg / "models" / "transformer.py").write_text(
        "print('flash_attn not installed')\nflex_attention_compiled = object()\n")

    torch = types.ModuleType("torch")
    torch.cleared = 0
    torch.cuda = types.SimpleNamespace(
        is_available=lambda: True,
        empty_cache=lambda: setattr(torch, "cleared", torch.cleared + 1))
    torch.from_numpy = lambda a: ("tensor", a.shape)
    monkeypatch.setitem(sys.modules, "torch", torch)
    for name in [m for m in sys.modules if m == "stable_audio_3" or m.startswith("stable_audio_3.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "path", list(sys.path))
    yield tmp_path / "stable-audio-3"
    for name in [m for m in sys.modules if m == "stable_audio_3" or m.startswith("stable_audio_3.")]:
        sys.modules.pop(name, None)


@pytest.fixture
def backend(upstream):
    return SA3TorchBackend(upstream_dir=upstream, model="medium")


def test_settings_and_stdout_kept_clean(backend, capsys):
    assert backend.name == "sa3-torch" and backend.device == "cuda"
    assert backend.model_label == "medium + same-l" and backend.max_seconds == 380
    assert not backend.loaded()
    backend._preload()
    assert backend.loaded() and backend.sample_rate == 44100
    assert sys.modules["stable_audio_3.model"].loads == [("medium", "cuda")]
    out = capsys.readouterr().out
    assert "stdout" not in out and "flash_attn" not in out


def test_small_models_map_to_upstream_names(upstream):
    b = SA3TorchBackend(upstream_dir=upstream, model="sm-sfx")
    assert b.upstream_model == "small-sfx" and b.max_seconds == 120 and b.model_label == "sm-sfx + same-s"
    b._preload()
    assert sys.modules["stable_audio_3.model"].loads == [("small-sfx", "cuda")]


def test_flex_attention_disabled_without_triton(backend, monkeypatch):
    import importlib.util
    has_triton = importlib.util.find_spec("triton") is not None
    assert (sys.modules["stable_audio_3.models.transformer"].flex_attention_compiled is None) != has_triton


def test_text_to_audio_one_call_per_seed(backend, capsys):
    calls = sys.modules["stable_audio_3.model"].calls
    r1 = backend._generate([5, 6], GenerationRequest(prompt="p", seconds=2, negative_prompt="noise"))
    r2 = backend._generate([7], GenerationRequest(prompt="p", seconds=3, cfg=4.0, negative_prompt="noise"))
    assert [c["seed"] for c in calls] == [5, 6, 7]
    assert calls[0]["steps"] == 8 and calls[0]["cfg_scale"] == 1.0 and calls[0]["apg_scale"] == 1.0
    assert calls[0]["sample_size"] == 16777216, "upstream's 120 s default would cut long tracks"
    assert "negative_prompt" not in calls[0], "negative prompt only applies with CFG"
    assert calls[2]["negative_prompt"] == "noise" and calls[2]["cfg_scale"] == 4.0
    assert "init_audio" not in calls[0] and "inpaint_audio" not in calls[0]
    assert r1.included_model_load and not r2.included_model_load
    assert len(r1.audios) == 2 and r1.audios[0].shape == (2, 88200)
    assert "stdout" not in capsys.readouterr().out


def test_edit_requests_map_to_upstream_kwargs(backend, tmp_path):
    src = tmp_path / "in.wav"
    sf.write(src, np.zeros((22050, 2), np.float32), 22050)
    calls = sys.modules["stable_audio_3.model"].calls
    backend._generate([1], GenerationRequest(prompt="p", seconds=4, init_audio=str(src), init_strength=0.4))
    assert calls[-1]["init_audio"] == (22050, ("tensor", (2, 22050)))
    assert calls[-1]["init_noise_level"] == 0.4
    backend._generate([1], GenerationRequest(prompt="p", seconds=6, inpaint_audio=str(src), inpaint_range=(4, 6)))
    assert calls[-1]["inpaint_audio"][0] == 22050
    assert calls[-1]["inpaint_mask_start_seconds"] == 4.0 and calls[-1]["inpaint_mask_end_seconds"] == 6.0
    assert "init_audio" not in calls[-1]


def test_unload_frees_cuda_cache(backend):
    backend._generate([1], GenerationRequest(prompt="p", seconds=1))
    assert backend._unload() is True and not backend.loaded()
    assert sys.modules["torch"].cleared == 1
    assert backend._unload() is False


def test_bad_dir_and_model(upstream, tmp_path):
    with pytest.raises(RuntimeError, match="install.ps1"):
        SA3TorchBackend(upstream_dir=tmp_path / "missing")
    with pytest.raises(RuntimeError, match="AUDIO_STUDIO_SA3_MODEL"):
        SA3TorchBackend(upstream_dir=upstream, model="large")


@pytest.mark.parametrize("system,machine,expected", [
    ("Darwin", "arm64", "sa3-mlx"),
    ("Windows", "AMD64", "sa3-torch"),
    ("Linux", "x86_64", "sa3-torch"),
])
def test_auto_backend(monkeypatch, system, machine, expected):
    monkeypatch.delenv("AUDIO_STUDIO_BACKEND", raising=False)
    monkeypatch.setattr("platform.system", lambda: system)
    monkeypatch.setattr("platform.machine", lambda: machine)
    assert resolve_backend_name() == expected
    assert resolve_backend_name("SA3-Torch") == "sa3-torch"
    with pytest.raises(RuntimeError, match="unknown"):
        resolve_backend_name("sa3-onnx")
