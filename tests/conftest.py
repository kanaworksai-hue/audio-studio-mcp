from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from audio_studio_mcp import server
from audio_studio_mcp.backends import Backend, GenerationRequest, GenerationResult


class FakeBackend(Backend):
    """Deterministic stand-in for a model: a seed-dependent sine per request."""
    name = "fake"
    model_label = "fake-model"
    sample_rate = 44100
    max_seconds = 60.0

    def __init__(self):
        super().__init__()
        self._pool = ThreadPoolExecutor(max_workers=1)
        self._loaded = False
        self.requests: list[tuple[list[int], GenerationRequest]] = []

    @property
    def executor(self):
        return self._pool

    def loaded(self) -> bool:
        return self._loaded

    def _generate(self, seeds, req):
        self.requests.append((list(seeds), req))
        first_load = not self._loaded
        self._loaded = True
        n = int(req.seconds * self.sample_rate)
        t = np.arange(n, dtype=np.float32) / self.sample_rate
        audios = [np.stack([1.2 * np.sin(2 * np.pi * (200 + s % 100) * t)] * 2).astype(np.float32)
                  for s in seeds]
        return GenerationResult(audios=audios, wall_seconds=0.01, included_model_load=first_load)

    def _unload(self):
        was, self._loaded = self._loaded, False
        return was

    def _preload(self):
        self._loaded = True


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def fake(tmp_path, monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(server, "OUTPUT_DIR", tmp_path / "out")
    server.set_backend(backend)
    yield backend
    server.set_backend(None)


def sine(seconds=1.0, sr=44100, freq=440.0, amp=0.5, channels=2):
    t = np.arange(int(seconds * sr), dtype=np.float32) / sr
    return np.stack([amp * np.sin(2 * np.pi * freq * t)] * channels).astype(np.float32)
